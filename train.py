"""
(CRNN) models training

Training strategy:
------------------
1. the following pairs of classes will be treated the same:
    (RBBB, CRBBB), (PAC, SVPB), (PVC, VPB)
    normalization of labels (classes) will be
    CRBB ---> RBBB, SVPB --- > PAC, VPB ---> PVC

2. the following classes will be determined by the special detectors:
    PR, LAD, RAD, LQRSV, Brady,
    (potentially) SB, STach

3. models will be trained for each tranche separatly:
    tranche A and B are from the same source, hence will be treated one during training,
    the distribution of the classes for each tranche are as follows:
        A+B: {'IAVB': 828, 'AF': 1374, 'AFL': 54, 'Brady': 271, 'CRBBB': 113, 'IRBBB': 86, 'LBBB': 274, 'NSIVCB': 4, 'PR': 3, 'PAC': 689, 'PVC': 188, 'LQT': 4, 'QAb': 1, 'RAD': 1, 'RBBB': 1858, 'SA': 11, 'SB': 45, 'NSR': 922, 'STach': 303, 'SVPB': 53, 'TAb': 22, 'TInv': 5, 'VPB': 8}
        C: {'AF': 2, 'Brady': 11, 'NSIVCB': 1, 'PAC': 3, 'RBBB': 2, 'SA': 2, 'STach': 11, 'SVPB': 4, 'TInv': 1}
        D: {'AF': 15, 'AFL': 1, 'NSR': 80, 'STach': 1}
        E: {'IAVB': 797, 'AF': 1514, 'AFL': 73, 'CRBBB': 542, 'IRBBB': 1118, 'LAnFB': 1626, 'LAD': 5146, 'LBBB': 536, 'LQRSV': 182, 'NSIVCB': 789, 'PR': 296, 'PAC': 398, 'LPR': 340, 'LQT': 118, 'QAb': 548, 'RAD': 343, 'SA': 772, 'SB': 637, 'NSR': 18092, 'STach': 826, 'SVPB': 157, 'TAb': 2345, 'TInv': 294}
        F: {'IAVB': 769, 'AF': 570, 'AFL': 186, 'Brady': 6, 'CRBBB': 28, 'IRBBB': 407, 'LAnFB': 180, 'LAD': 940, 'LBBB': 231, 'LQRSV': 374, 'NSIVCB': 203, 'PAC': 639, 'LQT': 1391, 'QAb': 464, 'RAD': 83, 'RBBB': 542, 'SA': 455, 'SB': 1677, 'NSR': 1752, 'STach': 1261, 'SVPB': 1, 'TAb': 2306, 'TInv': 812, 'VPB': 357}
    hence in this manner, training classes for each tranche are as follows:
        A+B: ['IAVB', 'AF', 'AFL',  'IRBBB', 'LBBB', 'PAC', 'PVC', 'RBBB', 'SB', 'NSR', 'STach', 'TAb']
        E: ['IAVB', 'AF', 'AFL', 'RBBB', 'IRBBB', 'LAnFB', 'LBBB', 'NSIVCB', 'PAC', 'LPR', 'LQT', 'QAb', 'SA', 'SB', 'NSR', 'STach', 'TAb', 'TInv']
        F: ['IAVB', 'AF', 'AFL', 'IRBBB', 'LAnFB', 'LBBB', 'NSIVCB', 'PAC', 'LQT', 'QAb', 'RBBB', 'SA', 'SB', 'NSR', 'STach', 'TAb', 'TInv', 'PVC']
    tranches C, D have too few recordings (recordings of C are long), which shall not be used to train separate models?

4. one model will be trained using the whole dataset (consider excluding tranche C? good news is that tranche C mainly consists of 'Brady' and 'STach', which can be classified using the special detectors)
        A+B+D+E+F: {'IAVB': 2394, 'AF': 3473, 'AFL': 314, 'Brady': 277, 'CRBBB': 683, 'IRBBB': 1611, 'LAnFB': 1806, 'LAD': 6086, 'LBBB': 1041, 'LQRSV': 556, 'NSIVCB': 996, 'PR': 299, 'PAC': 1726, 'PVC': 188, 'LPR': 340, 'LQT': 1513, 'QAb': 1013, 'RAD': 427, 'RBBB': 2400, 'SA': 1238, 'SB': 2359, 'NSR': 20846, 'STach': 2391, 'SVPB': 211, 'TAb': 4673, 'TInv': 1111, 'VPB': 365}
    hence classes for training are
        ['IAVB', 'AF', 'AFL', 'IRBBB', 'LAnFB', 'LBBB', 'NSIVCB', 'PAC', 'PVC', 'LPR', 'LQT', 'QAb', 'RBBB', 'SA', 'SB', 'NSR', 'STach', 'TAb', 'TInv']

"""
import os
import time
import logging
import argparse
from copy import deepcopy
from collections import deque
from typing import Union, Optional, Sequence, NoReturn
from numbers import Real, Number

from tqdm import tqdm
import numpy as np
import torch
from torch import nn
from torch import optim
from torch import Tensor
from torch.utils.data import DataLoader
import torch.nn.functional as F
from easydict import EasyDict as ED

from models.ecg_crnn import ECG_CRNN
from model_configs import ECG_CRNN_CONFIG
from cfg import ModelCfg, TrainCfg
from dataset import CINC2020
from utils.misc import init_logger, get_date_str


__all__ = [
    "train",
]


def train(model:nn.Module, device:torch.device, config:dict, log_step:int=20, logger:Optional[logging.Logger]=None):
    """

    Parameters:
    -----------
    model: Module,
    device: torch.device,
    config: dict,
    log_step: int, default 20,
    logger: Logger, optional,
    """
    train_dataset = CINC2020(config=config, training=True)
    val_dataset = CINC2020(config=config, training=False)

    n_train = len(train_dataset)
    n_val = len(val_dataset)

    n_epochs = config.n_epochs
    batch_size = config.batch_size
    lr = config.learning_rate

    train_loader = DataLoader(
        dataset=train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=8,
        pin_memory=True,
        drop_last=True,  # setting False would result in error
        collate_fn=collate_fn,
    )

    val_loader = DataLoader(
        dataset=val_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=8,
        pin_memory=True,
        drop_last=True,  # setting False would result in error
        collate_fn=collate_fn,
    )

    writer = SummaryWriter(
        log_dir=config.log_dir,
        filename_suffix=f'OPT_{model.__name__}_{config.cnn_name}_{config.train_optimizer}_LR_{lr}_BS_{batch_size}',
        comment=f'OPT_{model.__name__}_{config.cnn_name}_{config.train_optimizer}_LR_{lr}_BS_{batch_size}',
    )
    
    max_itr = n_epochs * n_train
    # global_step = cfg.TRAIN_MINEPOCH * n_train
    global_step = 0
    if logger:
        logger.info(f'''Starting training:
            Epochs:          {n_epochs}
            Batch size:      {batch_size}
            Learning rate:   {lr}
            Training size:   {n_train}
            Validation size: {n_val}
            Device:          {device.type}
            Optimizer:       {config.train_optimizer}
            Dataset classes: {config.classes}
        ''')

    # learning rate setup
    def burnin_schedule(i):
        if i < config.burn_in:
            factor = pow(i / config.burn_in, 4)
        elif i < config.steps[0]:
            factor = 1.0
        elif i < config.steps[1]:
            factor = 0.1
        else:
            factor = 0.01
        return factor

    if config.train_optimizer.lower() == 'adam':
        optimizer = optim.Adam(
            params=model.parameters(),
            lr=config.learning_rate / config.batch,
            betas=(0.9, 0.999),
            eps=1e-08,
        )
    elif config.train_optimizer.lower() == 'sgd':
        optimizer = optim.SGD(
            params=model.parameters(),
            lr=config.learning_rate / config.batch,
            momentum=config.momentum,
            weight_decay=config.decay,
        )
    scheduler = optim.lr_scheduler.LambdaLR(optimizer, burnin_schedule)

    if config.loss == "BCEWithLogitsLoss":
        criterion = nn.BCEWithLogitsLoss()
    else:
        raise NotImplementedError(f"loss `{config.loss}` not implemented!")
    # scheduler = ReduceLROnPlateau(optimizer, mode='max', verbose=True, patience=6, min_lr=1e-7)
    # scheduler = CosineAnnealingWarmRestarts(optimizer, 0.001, 1e-6, 20)

    save_prefix = f'{model.__name__}_epoch'
    saved_models = deque()
    model.train()

    for epoch in range(n_epochs):
        model.train()
        epoch_loss = 0
        epoch_step = 0

        with tqdm(total=n_train, desc=f'Epoch {epoch + 1}/{n_epochs}', ncols=100) as pbar:
            for i, batch in enumerate(train_loader):
                global_step += 1
                epoch_step += 1

                signals, labels = batch
                signals = signals.to(device=device, dtype=torch.float32)
                labels = labels.to(device=device)

                preds = model(signals)
                loss = criterion(preds, labels)
            #     loss, loss_xy, loss_wh, loss_obj, loss_cls, loss_l2 = criterion(bboxes_pred, bboxes)
            #     # loss = loss / config.subdivisions
            #     loss.backward()

            #     epoch_loss += loss.item()

            #     if global_step  % config.subdivisions == 0:
            #         optimizer.step()
            #         scheduler.step()
            #         model.zero_grad()

            #     if global_step % (log_step * config.subdivisions) == 0:
            #         writer.add_scalar('train/Loss', loss.item(), global_step)
            #         writer.add_scalar('train/loss_xy', loss_xy.item(), global_step)
            #         writer.add_scalar('train/loss_wh', loss_wh.item(), global_step)
            #         writer.add_scalar('train/loss_obj', loss_obj.item(), global_step)
            #         writer.add_scalar('train/loss_cls', loss_cls.item(), global_step)
            #         writer.add_scalar('train/loss_l2', loss_l2.item(), global_step)
            #         writer.add_scalar('lr', scheduler.get_lr()[0] * config.batch, global_step)
            #         pbar.set_postfix(**{
            #             'loss (batch)': loss.item(),
            #             'loss_xy': loss_xy.item(),
            #             'loss_wh': loss_wh.item(),
            #             'loss_obj': loss_obj.item(),
            #             'loss_cls': loss_cls.item(),
            #             'loss_l2': loss_l2.item(),
            #             'lr': scheduler.get_lr()[0] * config.batch
            #         })
            #         if logger:
            #             logger.info(f'Train step_{global_step}: loss : {loss.item()},loss xy : {loss_xy.item()}, loss wh : {loss_wh.item()}, loss obj : {loss_obj.item()}, loss cls : {loss_cls.item()}, loss l2 : {loss_l2.item()}, lr : {scheduler.get_lr()[0] * config.batch}')

            #     pbar.update(images.shape[0])
                
            # # TODO: eval for each epoch using `evaluate`
            # eval_model = Yolov4(yolov4conv137weight=None, n_classes=config.classes, inference=True)
            # eval_model.load_state_dict(model.state_dict())
            # eval_model.to(device)
            # evaluator = evaluate(eval_model, val_loader, config, device, logger)
            # del eval_model

            try:
                os.makedirs(config.checkpoints, exist_ok=True)
                if logger:
                    logger.info('Created checkpoint directory')
            except OSError:
                pass
            save_path = os.path.join(config.checkpoints, f'{save_prefix}{epoch + 1}_{get_date_str()}.pth')
            torch.save(model.state_dict(), save_path)
            if logger:
                logger.info(f'Checkpoint {epoch + 1} saved!')
            saved_models.append(save_path)
            # remove outdated models
            if len(saved_models) > config.keep_checkpoint_max > 0:
                model_to_remove = saved_models.popleft()
                try:
                    os.remove(model_to_remove)
                except:
                    logger.info(f'failed to remove {model_to_remove}')

    writer.close()


def collate_fn(batch:tuple) -> Tuple[Tensor, Tensor]:
    """ finished, Not checked,
    """
    signals = [[item[0]] for item in batch]
    labels = [[item[1]] for item in batch]
    signals = np.concatenate(signals, axis=0)
    images = torch.from_numpy(signals)
    labels = np.concatenate(labels, axis=0)
    labels = torch.from_numpy(labels)
    return signals, labels


@torch.no_grad()
def evaluate(model, data_loader, cfg, device, logger=None, **kwargs):
    """ NOT finished, NOT checked,
    """
    model.eval()

    raise NotImplementedError


def get_args(**kwargs):
    """
    """
    cfg = kwargs
    parser = argparse.ArgumentParser(
        description='Train the Model on CINC2020',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-l', '--learning-rate',
        metavar='LR', type=float, nargs='?', default=0.001,
        help='Learning rate',
        dest='learning_rate')
    parser.add_argument(
        '-g', '--gpu',
        metavar='G', type=str, default='0',
        help='GPU',
        dest='gpu')
    parser.add_argument(
        '-t', '--tranches',
        type=int, default=1,
        help='the tranches for training',
        dest='tranches_for_training')
    parser.add_argument(
        '-c', '--cnn-name',
        type=str, default='resnet',
        help='choice of cnn feature extractor',
        dest='cnn_name')
    parser.add_argument(
        '-keep-checkpoint-max', type=int, default=100,
        help='maximum number of checkpoints to keep. If set 0, all checkpoints will be kept',
        dest='keep_checkpoint_max')
    parser.add_argument(
        '-optimizer', type=str, default='adam',
        help='training optimizer',
        dest='TRAIN_OPTIMIZER')
    
    args = vars(parser.parse_args())

    cfg.update(args)
    
    return ED(cfg)



DAS = True

if __name__ == "__main__":
    cfg = get_args(**TrainCfg)
    # os.environ["CUDA_VISIBLE_DEVICES"] = cfg.gpu
    if not DAS:
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    else:
        device = torch.device('cuda')
    log_dir = cfg.TRAIN_TENSORBOARD_DIR
    logger = init_logger(log_dir=log_dir)
    logger.info(f"\n{'*'*20}   Start Training   {'*'*20}\n")
    logger.info(f'Using device {device}')
    logger.info(f"Using torch of version {torch.__version__}")
    logger.info(f'with configuration {cfg}')
    print(f"\n{'*'*20}   Start Training   {'*'*20}\n")
    print(f'Using device {device}')
    print(f"Using torch of version {torch.__version__}")
    print(f'with configuration {cfg}')

    tranches = cfg.tranche_for_training
    if tranches:
        classes = cfg.tranche_classes[tranches]
    else:
        classes = cfg.classes
    model_config = deepcopy(ECG_CRNN_CONFIG)
    model_config.cnn.name = cfg.cnn_name

    model = ECG_CRNN(classes=classes, config=model_config)

    if not DAS and torch.cuda.device_count() > 1:
        model = torch.nn.DataParallel(model)
    if not DAS:
        model.to(device=device)
    else:
        model.cuda()

    try:
        train(
            model=model,
            config=cfg,
            device=device,
            logger=logger,
        )
    except KeyboardInterrupt:
        torch.save(model.state_dict(), os.path.join(cfg.checkpoints, 'INTERRUPTED.pth'))
        logger.info('Saved interrupt')
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)

