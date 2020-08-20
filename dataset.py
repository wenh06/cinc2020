"""
"""
import os, sys
import json
from random import shuffle
from copy import deepcopy
from functools import reduce
from typing import Union, Optional, Tuple, Dict, Sequence, Set, NoReturn

import numpy as np
from easydict import EasyDict as ED
from tqdm import tqdm
import torch
from torch.utils.data.dataset import Dataset

from cfg import TrainCfg
from data_reader import CINC2020Reader as CR


__all__ = [
    "CINC2020",
]


class CINC2020(Dataset):
    """
    """
    def __init__(self, config:ED, tranches:Optional[str]=None, training:bool=True) -> NoReturn:
        """ finished, NOT checked,

        Parameters:
        -----------
        config: dict,
            configurations for the Dataset,
            ref. `cfg.TrainCfg`
        tranches: str, optional,
            tranches for training,
            can be one of "A", "B", "AB", "E", "F", or None (defaults to "ABEF")
        """
        super().__init__()
        self._TRANCHES = TrainCfg.tranche_classes.keys()  # ["A", "B", "AB", "E", "F"]
        self.config = deepcopy(config)
        self.reader = CR(db_dir=config.db_dir)
        self.tranches = tranches
        self.training = training
        assert not self.tranches or self.tranches in self._TRANCHES
        if self.tranches:
            self.all_classes = TrainCfg.tranche_classes[self.tranches]
        else:
            self.all_classes = TrainCfg.classes
        if self.training:
            self.siglen = TrainCfg.siglen
        else:
            self.siglen = None

        self.records = self._train_test_split(config.train_ratio, force_recompute=False)

    def __getitem__(self, index):
        """ finished, NOT checked,
        """
        rec = self.records[index]
        # values = self.reader.load_data(
        #     rec,
        #     data_format='channel_first', units='mV', backend='wfdb'
        # )
        
        values = self.reader.load_resampled_data(rec, siglen=self.siglen)
        labels = self.reader.get_labels(
            rec, scored_only=True, abbr=False, normalize=True
        )
        labels = [c for c in labels if c in self.all_classes]

        return values, labels

    def _get_val_item(self, index):
        """
        """
        raise NotImplementedError

    def __len__(self):
        """
        """
        return len(self.records)

    
    def _train_test_split(self, train_ratio:float=0.8, force_recompute:bool=False) -> List[str]:
        """ finished, NOT checked,

        Parameters:
        -----------
        train_ratio: float, default 0.8,
            ratio of the train set in the whole dataset (or the whole tranche(s))
        force_recompute: bool, default False,
            if True, force redo the train-test split,
            regardless of the existing ones stored in json files

        Returns:
        --------
        records: list of str,
            list of the records split for training or validation
        """
        _TRANCHES = list("ABEF")
        _train_ratio = int(train_ratio*100)
        _test_ratio = 100 - _train_ratio
        assert _train_ratio * _test_ratio > 0

        file_suffix = f"_siglen_{TrainCfg.input_len}.json"
        train_file = os.path.join(self.reader.db_dir_base, f"train_ratio_{_train_ratio}{file_suffix}")
        test_file = os.path.join(self.reader.db_dir_base, f"test_ratio_{_test_ratio}{file_suffix}")

        if force_recompute or not all([os.path.isfile(train_file), os.path.isfile(test_file)]):
            tranche_records = {t: [] for t in _TRANCHES}
            train = {t: [] for t in _TRANCHES}
            test = {t: [] for t in _TRANCHES}
            for t in _TRANCHES:
                with tqdm(self.reader.all_records[t], total=len(self.reader.all_records[t])) as bar:
                    for rec in bar:
                        rec_labels = self.reader.get_labels(rec, scored_only=True, fmt='a', normalize=True)
                        rec_labels = [c for c in rec_labels if c in TrainCfg.tranche_classes[t]]
                        if len(rec_labels) == 0:
                            continue
                        rec_samples = self.reader.load_resampled_data(rec).shape[1]
                        if rec_samples < TrainCfg.input_len:
                            continue
                        tranche_records[t].append(rec)
                    print(f"tranche {t} has {len(tranche_records[t])} valid records for training")
            for t in _TRANCHES:
                is_valid = False
                while not is_valid:
                    shuffle(tranche_records[t])
                    split_idx = int(len(tranche_records[t])*train_ratio)
                    train[t] = tranche_records[t][:split_idx]
                    test[t] = tranche_records[t][split_idx:]
                    is_valid = _check_train_test_split_validity(train[t], test[t], set(TrainCfg.tranche_classes[t]))
            with open(train_file, "w") as f:
                json.dump(train, f, ensure_ascii=False)
            with open(test_file, "w") as f:
                json.dump(test, f, ensure_ascii=False)
        else:
            with open(train_file, "r") as f:
                train = json.load(train_file)
            with open(test_file, "r") as f:
                test = json.load(test_file)

        add = lambda a,b:a+b
        _tranches = list(self.tranches or "ABEF")
        if self.training:
            records = reduce(add, [train[k] for k in _tranches])
        else:
            records = reduce(add, [test[k] for k in _tranches])
        return records


    def _check_train_test_split_validity(self, train:List[str], test:List[str], all_classes:Set[str]) -> bool:
        """ finished, checked,

        the train-test split is valid iff
        records in both `train` and `test` contain all classes in `all_classes`

        Parameters:
        -----------
        train: list of str,
            list of the records in the train set
        test: list of str,
            list of the records in the test set
        all_classes: set of str,
            the set of all classes for training

        Returns:
        --------
        is_valid: bool,
            the split is valid or not
        """
        add = lambda a,b:a+b
        train_classes = set(reduce(add, [self.reader.get_labels(rec, fmt='a') for rec in train]))
        train_classes.intersection_update(all_classes)
        test_classes = set(reduce(add, [self.reader.get_labels(rec, fmt='a') for rec in test]))
        test_classes.intersection_update(all_classes)
        is_valid = (len(all_classes) == len(train_classes) == len(test_classes))
        print(f"all_classes = {all_classes}\ntrain_classes = {train_classes}\ntest_classes = {test_classes}\nis_valid = {is_valid}")
        return is_valid
