import json
import warnings

import torch.optim as optim
from accelerate import Accelerator
from torch import nn
from torch.utils.data import DataLoader

from torchmetrics.functional import peak_signal_noise_ratio, structural_similarity_index_measure
from torchmetrics.image.lpip import LearnedPerceptualImagePatchSimilarity
from tqdm import tqdm

from config import Config
from data import get_data

from metrics.uciqe import batch_uciqe
from metrics.uiqm import batch_uiqm

from torchsampler import ImbalancedDatasetSampler

from models import *

from utils import *

warnings.filterwarnings('ignore')


def train():
    # Accelerate
    opt = Config('config.yml')
    seed_everything(opt.OPTIM.SEED)

    accelerator = Accelerator(log_with='wandb') if opt.OPTIM.WANDB else Accelerator()
    if accelerator.is_local_main_process:
        os.makedirs(opt.TRAINING.SAVE_DIR, exist_ok=True)
    device = accelerator.device

    config = {
        "dataset": opt.TRAINING.TRAIN_DIR
    }
    accelerator.init_trackers("UW", config=config)

    # Data Loader
    train_dir = opt.TRAINING.TRAIN_DIR
    val_dir = opt.TRAINING.VAL_DIR

    train_dataset = get_data(train_dir, opt.MODEL.INPUT, opt.MODEL.TARGET, 'train', opt.TRAINING.ORI,
                             {'w': opt.TRAINING.PS_W, 'h': opt.TRAINING.PS_H})
    trainloader = DataLoader(dataset=train_dataset, batch_size=opt.OPTIM.BATCH_SIZE, shuffle=True, num_workers=16,
                             drop_last=False, pin_memory=True)
    val_dataset = get_data(val_dir, opt.MODEL.INPUT, opt.MODEL.TARGET, 'test', opt.TRAINING.ORI,
                           {'w': opt.TRAINING.PS_W, 'h': opt.TRAINING.PS_H})
    testloader = DataLoader(dataset=val_dataset, batch_size=1, shuffle=False, num_workers=8, drop_last=False,
                            pin_memory=True)

    # Model & Loss
    model = Model()

    criterion_psnr = torch.nn.SmoothL1Loss()
    criterion_lpips = LearnedPerceptualImagePatchSimilarity(net_type='alex', normalize=True).to(device)

    # Optimizer & Scheduler
    optimizer_b = optim.AdamW(model.parameters(), lr=opt.OPTIM.LR_INITIAL, betas=(0.9, 0.999), eps=1e-8)
    scheduler_b = optim.lr_scheduler.CosineAnnealingLR(optimizer_b, opt.OPTIM.NUM_EPOCHS, eta_min=opt.OPTIM.LR_MIN)

    start_epoch = 1

    trainloader, testloader = accelerator.prepare(trainloader, testloader)
    model = accelerator.prepare(model)
    optimizer_b, scheduler_b = accelerator.prepare(optimizer_b, scheduler_b)

    best_epoch = 1
    best_psnr = 0

    size = len(testloader)

    # training
    for epoch in range(start_epoch, opt.OPTIM.NUM_EPOCHS + 1):
        model.train()

        for _, data in enumerate(tqdm(trainloader, disable=not accelerator.is_local_main_process)):
            inp = data[0].contiguous()
            tar = data[1]

            # forward
            optimizer_b.zero_grad()
            res = model(inp)

            loss_psnr = criterion_psnr(res, tar)
            loss_ssim = 1 - structural_similarity_index_measure(res, tar, data_range=1)
            loss_lpips = criterion_lpips(res, tar)

            train_loss = loss_psnr + 0.3 * loss_ssim + 0.7 * loss_lpips

            # backward
            accelerator.backward(train_loss)
            optimizer_b.step()

        scheduler_b.step()

        # testing
        if epoch % opt.TRAINING.VAL_AFTER_EVERY == 0:
            model.eval()
            psnr = 0
            ssim = 0
            lpips = 0

            uciqe = 0
            uiqm = 0

            for _, data in enumerate(tqdm(testloader, disable=not accelerator.is_local_main_process)):
                inp = data[0].contiguous()
                tar = data[1]

                with torch.no_grad():
                    res = model(inp)

                res, tar = accelerator.gather((res, tar))

                psnr += peak_signal_noise_ratio(res, tar, data_range=1).item()
                ssim += structural_similarity_index_measure(res, tar, data_range=1).item()
                lpips += criterion_lpips(res, tar).item()
                uciqe += batch_uciqe(res)
                uiqm += batch_uiqm(res)

            psnr /= size
            ssim /= size
            lpips /= size
            uciqe /= size
            uiqm /= size

            if psnr > best_psnr:
                # save model
                best_psnr = psnr
                best_epoch = epoch
                save_checkpoint({
                    'state_dict': model.state_dict(),
                }, epoch, opt.MODEL.SESSION, opt.TRAINING.SAVE_DIR)

            accelerator.log({
                "PSNR": psnr,
                "SSIM": ssim,
                "LPIPS": lpips,
                "UCIQE": uciqe,
                "UIQM": uiqm
            }, step=epoch)

            if accelerator.is_local_main_process:
                log_stats = ("epoch: {}, PSNR: {}, SSIM: {}, LPIPS: {}, UCIQE: {}, "
                             "UIQM: {}, best PSNR: {}, best epoch: {}"
                             .format(epoch, psnr, ssim, lpips, uciqe, uiqm, best_psnr, best_epoch))
                print(log_stats)
                with open(os.path.join(opt.LOG.LOG_DIR, opt.TRAINING.LOG_FILE), mode='a', encoding='utf-8') as f:
                    f.write(json.dumps(log_stats) + '\n')

    accelerator.end_training()


if __name__ == '__main__':
    train()
