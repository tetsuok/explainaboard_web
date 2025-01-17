from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta

import marisa_trie
from explainaboard.utils.cache_api import cache_online_file
from explainaboard.utils.typing_utils import unwrap
from explainaboard_web.models import DatasetMetadata, DatasetsReturn


class DatasetDB:
    def __init__(self, data: dict):
        """
        Create a dataset DB from a list of dictionaries as specified by the jsonl
        format in DataLab
        """
        self.name_dict: dict[str, list[int]] = {}
        self.task_dict: dict[str, list[int]] = {}
        self.id_dict: dict[str, int] = {}
        self.metadatas: list[DatasetMetadata] = []
        for metadata_id, (dataset_id, v_dataset) in enumerate(data.items()):
            # Names
            dataset_name = v_dataset["dataset_name"]
            sub_dataset = v_dataset.get("sub_dataset")
            if dataset_name not in self.name_dict:
                self.name_dict[dataset_name] = []
            self.name_dict[dataset_name].append(metadata_id)
            # Ids
            self.id_dict[dataset_id] = metadata_id
            # Tasks
            tasks = v_dataset.get("tasks")
            tasks = set([] if tasks is None else tasks)
            task_cats = v_dataset.get("task_categories")
            tasks = tasks.union(
                [] if task_cats is None else itertools.chain.from_iterable(task_cats)
            )
            for task in tasks:
                if task not in self.task_dict:
                    self.task_dict[task] = []
                self.task_dict[task].append(metadata_id)
            # Create document
            doc = {
                "dataset_id": dataset_id,
                "dataset_name": dataset_name,
                "sub_dataset": None if sub_dataset == "__NONE__" else sub_dataset,
                "split": v_dataset["splits"],
                "tasks": tasks,
                "languages": v_dataset.get("languages"),
                "custom_features": v_dataset.get("custom_features"),
            }
            self.metadatas.append(DatasetMetadata.from_dict(doc))
        self.name_trie = marisa_trie.Trie(self.name_dict.keys())


class DatasetDBUtils:

    online_path = "https://raw.githubusercontent.com/ExpressAI/DataLab/main/utils/dataset_info.jsonl"  # noqa
    _cached_db: DatasetDB | None = None
    _cached_time: datetime | None = None
    _cached_lifetime: timedelta = timedelta(hours=6)

    @staticmethod
    def get_dataset_db() -> DatasetDB:
        """
        Get a collection of datasets, indexed by dataset name, containing every
        sub_dataset
        """
        if DatasetDBUtils._cached_db is None or (
            datetime.now() - unwrap(DatasetDBUtils._cached_time)
            > DatasetDBUtils._cached_lifetime
        ):
            # Load the dataset db
            local_path = cache_online_file(
                DatasetDBUtils.online_path,
                "info/dataset_info.jsonl",
                lifetime=DatasetDBUtils._cached_lifetime,
            )
            datasets = {}
            with open(local_path) as fin:
                for line in fin:
                    data = json.loads(line)
                    for k, v in data.items():
                        # skip 'ERROR' or 'SKIPPED' entries
                        if isinstance(v, dict) and len(v) > 0:
                            datasets[k] = v
            # Create the DB
            DatasetDBUtils._cached_db = DatasetDB(data=datasets)
            DatasetDBUtils._cached_time = datetime.now()
        return DatasetDBUtils._cached_db

    @staticmethod
    def find_dataset_by_id(dataset_id: str) -> DatasetMetadata | None:
        my_db = DatasetDBUtils.get_dataset_db()
        metadata_id = my_db.id_dict.get(dataset_id)
        return my_db.metadatas[metadata_id] if metadata_id is not None else None

    @staticmethod
    def find_datasets(
        page: int = 0,
        page_size: int = 0,
        dataset_ids: list[str] | None = None,
        dataset_name: str | None = None,
        sub_dataset: str | None = None,
        task: str | None = None,
        no_limit: bool = False,
    ) -> DatasetsReturn:
        my_db = DatasetDBUtils.get_dataset_db()
        metadata_ids: set[int] | None = None
        if dataset_ids is not None:
            metadata_ids = set()
            for dataset_id in dataset_ids:
                if dataset_id in my_db.id_dict:
                    metadata_ids.add(my_db.id_dict[dataset_id])
        if dataset_name is not None:
            found_items = my_db.name_trie.keys(dataset_name)
            found_ids = [my_db.name_dict[x] for x in found_items]
            chained_ids = list(itertools.chain.from_iterable(found_ids))
            metadata_ids = (
                metadata_ids.intersection(chained_ids)
                if metadata_ids
                else set(chained_ids)
            )
        if task is not None:
            # change the rule for search task from exact match to partly match.
            task_ids = []
            for name in my_db.task_dict.keys():
                if task in name:

                    task_ids += my_db.task_dict.get(name, [])

            # task_ids = my_db.task_dict.get(task, [])
            metadata_ids = (
                metadata_ids.intersection(task_ids) if metadata_ids else set(task_ids)
            )
        if sub_dataset is not None:
            new_metadata_ids = set()
            for id in metadata_ids or my_db.id_dict.values():
                if my_db.metadatas[id].sub_dataset == sub_dataset:
                    new_metadata_ids.add(id)
            metadata_ids = new_metadata_ids
        sid, eid = page * page_size, (page + 1) * page_size
        my_values = (
            my_db.metadatas
            if metadata_ids is None
            else [my_db.metadatas[x] for x in metadata_ids]
        )
        total = len(my_values)
        examps = my_values if (no_limit or page_size == 0) else my_values[sid:eid]
        return DatasetsReturn(examps, total)
