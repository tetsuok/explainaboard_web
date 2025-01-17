from __future__ import annotations

import binascii
import datetime
import json
import logging
import os
from functools import lru_cache

import pandas as pd
from explainaboard import DatalabLoaderOption, TaskType, get_processor
from explainaboard.analysis.analyses import BucketAnalysis
from explainaboard.analysis.case import AnalysisCase
from explainaboard.info import SysOutputInfo
from explainaboard.loaders import get_loader_class
from explainaboard.metrics.metric import SimpleMetricStats
from explainaboard.serialization.legacy import general_to_dict
from explainaboard.utils.cache_api import get_cache_dir, open_cached_file, sanitize_path
from explainaboard.utils.typing_utils import narrow
from explainaboard_web.impl.analyses.significance_analysis import (
    pairwise_significance_test,
)
from explainaboard_web.impl.auth import User, get_user
from explainaboard_web.impl.benchmark_utils import BenchmarkUtils
from explainaboard_web.impl.db_utils.dataset_db_utils import DatasetDBUtils
from explainaboard_web.impl.db_utils.system_db_utils import SystemDBUtils
from explainaboard_web.impl.language_code import get_language_codes
from explainaboard_web.impl.private_dataset import is_private_dataset
from explainaboard_web.impl.tasks import get_task_categories
from explainaboard_web.impl.utils import abort_with_error_message, decode_base64
from explainaboard_web.models import (
    Benchmark,
    BenchmarkConfig,
    DatasetMetadata,
    SingleAnalysis,
    SystemOutput,
)
from explainaboard_web.models.datasets_return import DatasetsReturn
from explainaboard_web.models.language_code import LanguageCode
from explainaboard_web.models.system import System
from explainaboard_web.models.system_analyses_return import SystemAnalysesReturn
from explainaboard_web.models.system_create_props import SystemCreateProps
from explainaboard_web.models.system_info import SystemInfo
from explainaboard_web.models.system_update_props import SystemUpdateProps
from explainaboard_web.models.systems_analyses_body import SystemsAnalysesBody
from explainaboard_web.models.systems_return import SystemsReturn
from explainaboard_web.models.task import Task
from explainaboard_web.models.task_category import TaskCategory
from flask import current_app
from pymongo import ASCENDING, DESCENDING


def _is_creator(system: System, user: User) -> bool:
    """check if a user is the creator of a system"""
    return system.creator == user.email


def _is_shared_user(system: System, user: User) -> bool:
    """check if a user is a shared user of a system"""
    return system.shared_users and user.email in system.shared_users


def _has_write_access(system: System) -> bool:
    """check if the current user has write access of a system"""
    user = get_user()
    return user.is_authenticated and _is_creator(system, user)


def _has_read_access(system: System) -> bool:
    """check if the current user has read access of a system"""
    user = get_user()
    return not system.is_private or (
        user.is_authenticated
        and (_is_creator(system, user) or _is_shared_user(system, user))
    )


""" /info """


@lru_cache(maxsize=None)
def info_get():
    api_version = None
    with open("explainaboard_web/swagger/swagger.yaml") as f:
        for line in f:
            if line.startswith("  version: "):
                api_version = line[len("version: ") + 1 : -1].strip()
                break
    if not api_version:
        raise RuntimeError("failed to extract API version")
    return {
        "env": os.getenv("FLASK_ENV"),
        "auth_url": current_app.config.get("AUTH_URL"),
        "api_version": api_version,
    }


""" /user """


def user_get():
    user = get_user()
    if not user:
        abort_with_error_message(401, "login required")
    return user.get_user_info()


""" /tasks """


def tasks_get() -> list[TaskCategory]:
    _categories = get_task_categories()
    categories: list[TaskCategory] = []
    for _category in _categories:
        tasks: list[Task] = []
        for _task in _category.tasks:
            loader_class = get_loader_class(_task.name)
            supported_formats = loader_class.supported_file_types()
            supported_metrics = [
                metric.name for metric in get_processor(_task.name).full_metric_list()
            ]
            tasks.append(
                Task(
                    _task.name, _task.description, supported_metrics, supported_formats
                )
            )
        categories.append(TaskCategory(_category.name, _category.description, tasks))
    return categories


""" /languagecodes """


def language_codes_get() -> list[LanguageCode]:
    language_codes = get_language_codes()
    return language_codes


""" /datasets """


def datasets_dataset_id_get(dataset_id: str) -> DatasetMetadata:
    dataset = DatasetDBUtils.find_dataset_by_id(dataset_id)
    if dataset is None:
        abort_with_error_message(404, f"dataset id: {dataset_id} not found")
    return dataset


def datasets_get(
    dataset_ids: str | None,
    dataset_name: str | None,
    task: str | None,
    page: int,
    page_size: int,
) -> DatasetsReturn:
    parsed_dataset_ids = dataset_ids.split(",") if dataset_ids else None
    return DatasetDBUtils.find_datasets(
        page=page,
        page_size=page_size,
        dataset_ids=parsed_dataset_ids,
        dataset_name=dataset_name,
        task=task,
    )


""" /benchmarks """


def benchmark_configs_get(parent: str | None) -> list[BenchmarkConfig]:
    scriptpath = os.path.dirname(__file__)
    config_folder = os.path.join(scriptpath, "./benchmark_configs/")
    # Add benchmarks to here if they should be displayed on the page.
    # This should perhaps be moved to the database or made dynamic later.
    # display_benchmarks = ["masakhaner", "gaokao"]
    # Get all benchmark configs
    """
    display_benchmarks = ["masakhaner", "gaokao"]
    # Get all benchmark configs
    benchmark_configs = [
        BenchmarkUtils.config_from_json_file(f"{config_folder}/config_{x}.json")
        for x in display_benchmarks
    ]
    """
    benchmark_configs = []
    for file_name in sorted(os.listdir(config_folder)):
        if file_name.endswith(".json"):
            benchmark_dict = BenchmarkUtils.config_dict_from_file(
                config_folder + file_name
            )
            # must match parent if one exists
            if (parent or "") == (benchmark_dict.get("parent", "")):
                benchmark_configs.append(BenchmarkConfig.from_dict(benchmark_dict))

    return benchmark_configs


def benchmark_get_by_id(benchmark_id: str, by_creator: bool) -> Benchmark:
    config = BenchmarkConfig.from_dict(BenchmarkUtils.config_dict_from_id(benchmark_id))
    if config.type == "abstract":
        return Benchmark(config, None, None)
    file_path = benchmark_id + "_benchmark.json"
    benchmark_file = open_cached_file(file_path, datetime.timedelta(seconds=1))
    if not benchmark_file:
        sys_infos = BenchmarkUtils.load_sys_infos(config)
        orig_df = BenchmarkUtils.generate_dataframe_from_sys_infos(config, sys_infos)

        system_dfs = BenchmarkUtils.generate_view_dataframes(
            config, orig_df, by_creator=False
        )
        system_dict = {k: v.to_dict() for k, v in system_dfs}
        creator_dfs = BenchmarkUtils.generate_view_dataframes(
            config, orig_df, by_creator=True
        )
        creator_dict = {k: v.to_dict() for k, v in creator_dfs}

        json_dict = {"system": system_dict, "creator": creator_dict}
        benchmark_file = os.path.join(get_cache_dir(), sanitize_path(file_path))
        with open(benchmark_file, "w") as outfile:
            json.dump(json_dict, outfile)
    update_time = (
        datetime.datetime.fromtimestamp(os.path.getmtime(benchmark_file))
    ).strftime("%m-%d-%Y %H:%M:%S")
    f = open(benchmark_file)
    if by_creator:
        view_dict = json.load(f)["creator"]
    else:
        view_dict = json.load(f)["system"]
    plot_dict = BenchmarkUtils.generate_plots(benchmark_id)
    views = []
    for k, v in view_dict.items():
        if by_creator:
            col_name = "creator"
        elif k == "Most-underserved Languages":
            col_name = "source_language"
        else:
            col_name = "system_name"
        views.append(
            BenchmarkUtils.dataframe_to_table(
                k, pd.DataFrame.from_dict(v), plot_dict, col_name
            )
        )
    return Benchmark(config, views, update_time)


""" /systems """


def systems_get_by_id(system_id: str) -> System:
    system = SystemDBUtils.find_system_by_id(system_id)
    if not _has_read_access(system):
        abort_with_error_message(403, "system access denied", 40302)
    return system


def systems_get(
    system_name: str | None,
    task: str | None,
    dataset: str | None,
    subdataset: str | None,
    split: str | None,
    page: int,
    page_size: int,
    sort_field: str,
    sort_direction: str,
    creator: str | None,
    shared_users: list[str] | None,
) -> SystemsReturn:
    ids = None
    if not sort_field:
        sort_field = "created_at"
    if not sort_direction:
        sort_direction = "desc"
    if sort_direction not in ["asc", "desc"]:
        abort_with_error_message(400, "sort_direction needs to be one of asc or desc")
    if sort_field != "created_at":
        sort_field = f"system_info.results.overall.{sort_field}.value"

    dir = ASCENDING if sort_direction == "asc" else DESCENDING

    return SystemDBUtils.find_systems(
        page=page,
        page_size=page_size,
        ids=ids,
        system_name=system_name,
        task=task,
        dataset_name=dataset,
        subdataset_name=subdataset,
        split=split,
        sort=[(sort_field, dir)],
        creator=creator,
        shared_users=shared_users,
    )


def systems_post(body: SystemCreateProps) -> System:
    """
    aborts with error if fails
    TODO: error handling
    """
    if body.metadata.dataset_metadata_id:
        if not body.metadata.dataset_split:
            abort_with_error_message(
                400, "dataset split is required if a dataset is chosen"
            )
        if body.custom_dataset:
            abort_with_error_message(
                400,
                "both datalab dataset and custom dataset are "
                "provided. please only select one.",
            )

    try:
        body.system_output.data = decode_base64(body.system_output.data)
        if body.custom_dataset and body.custom_dataset.data:
            body.custom_dataset.data = decode_base64(body.custom_dataset.data)
        system = SystemDBUtils.create_system(
            body.metadata, body.system_output, body.custom_dataset
        )
        return system
    except binascii.Error as e:
        abort_with_error_message(
            400, f"file should be sent in plain text base64. ({e})"
        )


def systems_update_by_id(body: SystemUpdateProps, system_id: str):
    system = SystemDBUtils.find_system_by_id(system_id)
    if not _has_write_access(system):
        abort_with_error_message(403, "system update denied", 40303)

    success = SystemDBUtils.update_system_by_id(system_id, body.metadata)
    if success:
        return "Success"
    abort_with_error_message(400, f"failed to update system {system_id}")


def system_outputs_get_by_id(
    system_id: str, output_ids: list[int] | None
) -> list[SystemOutput]:
    """
    TODO: return special error/warning if some ids cannot be found
    """
    system = SystemDBUtils.find_system_by_id(system_id)
    if not _has_read_access(system):
        abort_with_error_message(403, "system access denied", 40302)
    if is_private_dataset(
        DatalabLoaderOption(
            system.system_info.dataset_name,
            system.system_info.sub_dataset_name,
            system.system_info.dataset_split,
        )
    ):
        abort_with_error_message(
            403, f"{system.system_info.dataset_name} is a private dataset", 40301
        )

    return SystemDBUtils.find_system_outputs(system_id, output_ids)


def system_cases_get_by_id(
    system_id: str,
    level: int,
    case_ids: list[int] | None,
) -> list[AnalysisCase]:
    """
    TODO: return special error/warning if some ids cannot be found
    """
    system = SystemDBUtils.find_system_by_id(system_id)
    if not _has_read_access(system):
        abort_with_error_message(403, "system access denied", 40302)
    if is_private_dataset(
        DatalabLoaderOption(
            system.system_info.dataset_name,
            system.system_info.sub_dataset_name,
            system.system_info.dataset_split,
        )
    ):
        abort_with_error_message(
            403, f"{system.system_info.dataset_name} is a private dataset", 40301
        )

    return SystemDBUtils.find_analysis_cases(system_id, level=level, case_ids=case_ids)


def systems_delete_by_id(system_id: str):
    success = SystemDBUtils.delete_system_by_id(system_id)
    if success:
        return "Success"
    abort_with_error_message(400, f"cannot find system_id: {system_id}")


def systems_analyses_post(body: SystemsAnalysesBody):
    system_ids_str = body.system_ids
    feature_to_bucket_info = body.feature_to_bucket_info

    system_analyses: list[SingleAnalysis] = []
    system_ids: list = system_ids_str.split(",")
    system_name = None
    task = None
    dataset_name = None
    subdataset_name = None
    split = None
    creator = None
    shared_users = None
    page = 0
    page_size = len(system_ids)
    sort = None
    systems: list[System] = SystemDBUtils.find_systems(
        ids=system_ids,
        page=page,
        page_size=page_size,
        task=task,
        system_name=system_name,
        dataset_name=dataset_name,
        subdataset_name=subdataset_name,
        split=split,
        sort=sort,
        creator=creator,
        shared_users=shared_users,
        include_metric_stats=True,
    ).systems
    systems_len = len(systems)
    if systems_len == 0:
        return SystemAnalysesReturn(system_analyses)

    # performance significance test if there are two systems
    sig_info = []
    if len(systems) == 2:

        system1_info: SystemInfo = systems[0].system_info
        system1_info_dict = general_to_dict(system1_info)
        system1_output_info = SysOutputInfo.from_dict(system1_info_dict)

        system1_metric_stats: list[SimpleMetricStats] = [
            SimpleMetricStats(stat) for stat in systems[0].metric_stats[0]
        ]

        system2_info: SystemInfo = systems[1].system_info
        system2_info_dict = general_to_dict(system2_info)
        system2_output_info = SysOutputInfo.from_dict(system2_info_dict)
        system2_metric_stats: list[SimpleMetricStats] = [
            SimpleMetricStats(stat) for stat in systems[1].metric_stats[0]
        ]

        sig_info = pairwise_significance_test(
            system1_output_info,
            system2_output_info,
            system1_metric_stats,
            system2_metric_stats,
        )

    for system in systems:
        system_info: SystemInfo = system.system_info
        system_info_dict = general_to_dict(system_info)
        system_output_info = SysOutputInfo.from_dict(system_info_dict)

        for analysis in system_output_info.analyses:
            if (
                isinstance(analysis, BucketAnalysis)
                and analysis.feature in feature_to_bucket_info
            ):
                # The "fixed" method is required for SDK to perform
                # custom-interval analysis
                analysis.method = "fixed"
                analysis.number = feature_to_bucket_info[analysis.feature].number
                # Convert interval to type tuple so it becomes hashable,
                # as required by SDK
                analysis.setting = [
                    (interval[0], interval[1])
                    for interval in feature_to_bucket_info[analysis.feature].setting
                ]

        logging.getLogger().warning(
            "user-defined bucket analyses are not " "re-implemented"
        )

        processor = get_processor(TaskType(system_output_info.task_name))
        metric_stats = [[SimpleMetricStats(y) for y in x] for x in system.metric_stats]

        # Get analysis cases
        analysis_cases = []
        case_ids = None
        for i, analysis_level in enumerate(system.system_info.analysis_levels):
            level_cases = SystemDBUtils.find_analysis_cases(
                system_id=system.system_id,
                level=analysis_level.name,
                case_ids=case_ids,
            )
            # Note we are casting here, as SystemOutput.from_dict() actually just
            # returns a dict
            level_cases = [AnalysisCase.from_dict(narrow(dict, x)) for x in level_cases]
            analysis_cases.append(level_cases)

        processor_result = processor.perform_analyses(
            system_output_info,
            analysis_cases,
            metric_stats,
            skip_failed_analyses=True,
        )
        single_analysis = SingleAnalysis(analysis_results=processor_result)
        system_analyses.append(single_analysis)

    return SystemAnalysesReturn(system_analyses, sig_info)
