from __future__ import annotations

import traceback
from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from explainaboard import (
    DatalabLoaderOption,
    FileType,
    Source,
    TaskType,
    get_custom_dataset_loader,
    get_datalab_loader,
    get_processor,
)
from explainaboard.processors.processor_registry import get_metric_list_for_processor
from explainaboard_web.impl.auth import get_user
from explainaboard_web.impl.db_utils.dataset_db_utils import DatasetDBUtils
from explainaboard_web.impl.db_utils.db_utils import DBCollection, DBUtils
from explainaboard_web.impl.utils import (
    abort_with_error_message,
    binarize_bson,
    unbinarize_bson,
)
from explainaboard_web.models import (
    DatasetMetadata,
    System,
    SystemCreateProps,
    SystemOutput,
    SystemOutputProps,
    SystemOutputsReturn,
    SystemsReturn,
)
from pymongo.client_session import ClientSession


class SystemDBUtils:
    @staticmethod
    def system_from_dict(
        dikt: dict[str, Any], include_metric_stats: bool = False
    ) -> System:
        document: dict[str, Any] = {**dikt}
        if dikt.get("_id"):
            document["system_id"] = str(dikt["_id"])
        if dikt.get("is_private") is None:
            document["is_private"] = True
        if dikt.get("dataset_metadata_id") and dikt.get("dataset") is None:
            dataset = DBUtils.find_one_by_id(
                DBUtils.DATASET_METADATA, dikt["dataset_metadata_id"]
            )
            if dataset:
                split = document.get("dataset_split")
                # this check only valid for create
                if split and split not in dataset["split"]:
                    abort_with_error_message(
                        400, f"{split} is not a valid split for {dataset.dataset_name}"
                    )
                dataset["dataset_id"] = str(dataset.pop("_id"))
                document["dataset"] = dataset
            dikt.pop("dataset_metadata_id")

        metric_stats = []
        if "metric_stats" in document:
            metric_stats = document["metric_stats"]
            document["metric_stats"] = []
        system = System.from_dict(document)
        if include_metric_stats:
            # Unbinarize to numpy array and set explicitly
            system.metric_stats = [unbinarize_bson(stat) for stat in metric_stats]
        return system

    @staticmethod
    def find_systems(
        ids: Optional[list[str]],
        page: int,
        page_size: int,
        system_name: Optional[str],
        task: Optional[str],
        dataset_name: Optional[str],
        subdataset_name: Optional[str],
        split: Optional[str],
        sort: Optional[list],
        creator: Optional[str],
        include_datasets: bool = False,
        include_metric_stats: bool = False,
    ) -> SystemsReturn:
        """find multiple systems that matches the filters"""

        filt: dict[str, Any] = {}
        if ids:
            filt["_id"] = {"$in": [ObjectId(_id) for _id in ids]}
        if system_name:
            filt["model_name"] = {"$regex": rf"^{system_name}.*"}
        if task:
            filt["system_info.task_name"] = task
        if dataset_name:
            filt["system_info.dataset_name"] = dataset_name
        if subdataset_name:
            filt["system_info.sub_dataset_name"] = subdataset_name
        if split:
            filt["system_info.dataset_split"] = split
        if creator:
            filt["creator"] = creator
        filt["$or"] = [{"is_private": False}]
        if get_user().is_authenticated:
            filt["$or"].append({"creator": get_user().email})

        cursor, total = DBUtils.find(
            DBUtils.DEV_SYSTEM_METADATA, filt, sort, page * page_size, page_size
        )
        documents = list(cursor)

        systems: list[System] = []
        if len(documents) == 0:
            return SystemsReturn(systems, 0)

        # query datasets in batch to make it more efficient
        dataset_dict: dict[str, DatasetMetadata] = {}
        if include_datasets:
            dataset_ids: list[str] = []
            for doc in documents:
                if doc.get("dataset_metadata_id"):
                    dataset_ids.append(doc["dataset_metadata_id"])
            datasets = DatasetDBUtils.find_datasets(
                page=0, page_size=0, dataset_ids=dataset_ids, no_limit=True
            ).datasets
            for dataset in datasets:
                dataset_dict[dataset.dataset_id] = dataset

        for doc in documents:
            if not include_datasets or "dataset_metadata_id" not in doc:
                doc.pop("dataset_metadata_id", None)
            else:
                dataset = dataset_dict.get(doc["dataset_metadata_id"])
                if dataset:
                    doc["dataset"] = {
                        "dataset_id": dataset.dataset_id,
                        "dataset_name": dataset.dataset_name,
                        "sub_dataset": dataset.sub_dataset,
                        "tasks": dataset.tasks,
                    }
                else:
                    doc.pop("dataset_metadata_id", None)
                doc.pop("dataset_metadata_id")
            doc["system_id"] = doc.pop("_id")
            system = SystemDBUtils.system_from_dict(
                doc, include_metric_stats=include_metric_stats
            )
            systems.append(system)

        return SystemsReturn(systems, total)

    @staticmethod
    def create_system(
        metadata: SystemCreateProps,
        system_output: SystemOutputProps,
        custom_dataset: SystemOutputProps | None = None,
    ) -> System:
        """
        create a system
          1. validate and initialize a SystemModel
          2. load system_output data
          3. generate analysis report from system_output
          -- DB --
          5. write to system_metadata
            (metadata + sufficient statistics + overall analysis)
          6. write to system_outputs
        """
        system = SystemDBUtils.system_from_dict(metadata.to_dict())

        user = get_user()
        system.creator = user.email

        # validation
        if metadata.dataset_metadata_id:
            if not system.dataset:
                abort_with_error_message(
                    400, f"dataset: {metadata.dataset_metadata_id} does not exist"
                )
            if metadata.task not in system.dataset.tasks:
                abort_with_error_message(
                    400,
                    f"dataset {system.dataset.dataset_name} cannot be used for "
                    f"{metadata.task} tasks",
                )

        def load_sys_output():
            if custom_dataset:
                return get_custom_dataset_loader(
                    task=metadata.task,
                    dataset_data=custom_dataset.data,
                    output_data=system_output.data,
                    dataset_source=Source.in_memory,
                    output_source=Source.in_memory,
                    dataset_file_type=FileType(custom_dataset.file_type),
                    output_file_type=FileType(system_output.file_type),
                ).load()
            else:
                return get_datalab_loader(
                    task=metadata.task,
                    dataset=DatalabLoaderOption(
                        system.dataset.dataset_name,
                        system.dataset.sub_dataset,
                        metadata.dataset_split,
                    ),
                    output_data=system_output.data,
                    output_file_type=FileType(system_output.file_type),
                    output_source=Source.in_memory,
                ).load()

        def process():
            processor = get_processor(metadata.task)
            metrics_lookup = {
                metric.name: metric
                for metric in get_metric_list_for_processor(TaskType(metadata.task))
            }
            metric_configs = []
            for metric_name in metadata.metric_names:
                if metric_name not in metrics_lookup:
                    abort_with_error_message(
                        400, f"{metric_name} is not a supported metric"
                    )
                metric_configs.append(metrics_lookup[metric_name])
            processor_metadata = {
                **metadata.to_dict(),
                "task_name": metadata.task,
                "dataset_name": system.dataset.dataset_name if system.dataset else None,
                "sub_dataset_name": system.dataset.sub_dataset
                if system.dataset
                else None,
                "dataset_split": metadata.dataset_split,
                "metric_configs": metric_configs,
            }

            return processor.get_overall_statistics(
                metadata=processor_metadata, sys_output=system_output_data
            )

        try:
            system_output_data = load_sys_output()
            overall_statistics = process()
            metric_stats = [
                binarize_bson(metric_stat.get_data())
                for metric_stat in overall_statistics.metric_stats
            ]

            # TODO(chihhao) needs proper serializiation & deserializiation in SDK
            overall_statistics.sys_info.tokenizer = (
                overall_statistics.sys_info.tokenizer.json_repr()
            )
            # TODO avoid None as nullable seems undeclarable for array and object
            # in openapi.yaml
            if overall_statistics.sys_info.results.calibration is None:
                overall_statistics.sys_info.results.calibration = []
            if overall_statistics.sys_info.results.fine_grained is None:
                overall_statistics.sys_info.results.fine_grained = {}

            system.system_info = overall_statistics.sys_info.to_dict()
            system.metric_stats = metric_stats
            system.active_features = overall_statistics.active_features

            def db_operations(session: ClientSession) -> str:
                # Insert system
                system.created_at = system.last_modified = datetime.utcnow()
                document = system.to_dict()
                document.pop("system_id")
                document["dataset_metadata_id"] = (
                    system.dataset.dataset_id if system.dataset else None
                )
                document.pop("dataset")
                system_db_id = DBUtils.insert_one(DBUtils.DEV_SYSTEM_METADATA, document)
                # Insert system output
                output_collection = DBCollection(
                    db_name=DBUtils.SYSTEM_OUTPUT_DB, collection_name=str(system_db_id)
                )
                DBUtils.drop(output_collection)
                DBUtils.insert_many(
                    output_collection, list(system_output_data), False, session
                )
                return system_db_id

            system_id = DBUtils.execute_transaction(db_operations)

            # Metric_stats is replaced with empty list for now as
            # bson's Binary and numpy array is not directly serializable
            # in swagger. If the frontend needs it,
            # we can come up with alternatives.
            system.metric_stats = []
            system.system_id = system_id
            return system
        except ValueError as e:
            traceback.print_exc()
            abort_with_error_message(400, str(e))
            # mypy doesn't seem to understand the NoReturn type in an except block.
            # It's a noop to fix it
            raise e

    @staticmethod
    def find_system_outputs(
        system_id: str, output_ids: str | None, limit=0
    ) -> SystemOutputsReturn:
        """
        find multiple system outputs whose ids are in output_ids
        TODO: raise error if system doesn't exist
        """
        filt: dict[str, Any] = {}
        if output_ids:
            filt["id"] = {"$in": [str(id) for id in output_ids.split(",")]}
        output_collection = DBCollection(
            db_name=DBUtils.SYSTEM_OUTPUT_DB, collection_name=str(system_id)
        )
        cursor, total = DBUtils.find(
            collection=output_collection, filt=filt, limit=limit
        )
        return SystemOutputsReturn(
            [SystemOutput.from_dict(doc) for doc in cursor], total
        )

    @staticmethod
    def delete_system_by_id(system_id: str):
        user = get_user()
        if not user.is_authenticated:
            abort_with_error_message(401, "log in required")

        def db_operations(session: ClientSession) -> bool:
            """TODO: add logging if error"""
            sys = DBUtils.find_one_by_id(
                DBUtils.DEV_SYSTEM_METADATA,
                system_id,
            )
            if not sys:
                return False
            if sys["creator"] != user.email:
                abort_with_error_message(403, "you can only delete your own systems")
            result = DBUtils.delete_one_by_id(
                DBUtils.DEV_SYSTEM_METADATA, system_id, session=session
            )
            if not result:
                return False
            # drop cannot be added to a multi-document transaction, this seems
            # fine because drop is the last operation. If drop fails, delete
            # gets rolled back which is our only requirement here.
            output_collection = DBCollection(
                db_name=DBUtils.SYSTEM_OUTPUT_DB, collection_name=str(system_id)
            )
            DBUtils.drop(output_collection)
            return True

        return DBUtils.execute_transaction(db_operations)
