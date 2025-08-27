import torch
import torch.nn as nn
import torch.nn.functional as F

# ESR loss calculates the Error-to-signal between the output/target
class ESRLoss(nn.Module):
    def __init__(self):
        super(ESRLoss, self).__init__()
        self.epsilon = 0.00001

    def forward(self, output, target):
        loss = torch.add(target, -output)
        loss = torch.pow(loss, 2)
        loss = torch.mean(loss)
        energy = torch.mean(torch.pow(target, 2)) + self.epsilon
        loss = torch.div(loss, energy)
        return loss


class DCLoss(nn.Module):
    def __init__(self):
        super(DCLoss, self).__init__()
        self.epsilon = 0.00001

    def forward(self, output, target):
        loss = torch.pow(torch.add(torch.mean(target, 0), -torch.mean(output, 0)), 2)
        loss = torch.mean(loss)
        energy = torch.mean(torch.pow(target, 2)) + self.epsilon
        loss = torch.div(loss, energy)
        return loss

# ESR loss calculates the Error-to-signal between the output/target
class MultiSpecLoss(nn.Module):
    def __init__(self, fft_sizes=(2048, 1024, 512, 256, 128)):
        super(MultiSpecLoss, self).__init__()
        self.epsilon = 0.00001
        self.fft_sizes = fft_sizes
        self.spec_loss = []
        for size in self.fft_sizes:
            hop = size//4
            self.spec_loss.append(SpecLoss(size, hop))

    def forward(self, output, target):
        output = output.squeeze()
        target = target.squeeze()
        total_loss = 0
        for item in self.spec_loss:
            total_loss += item(output, target)
        return total_loss/len(self.fft_sizes)

class SpecLoss(nn.Module):
    def __init__(self, fft_size=512, hop_size=128):
        super(SpecLoss, self).__init__()
        self.epsilon = 0.00001
        self.fft_size = fft_size
        self.hop_size = hop_size

    def forward(self, output, target):
        magx = torch.abs(torch.stft(output, n_fft=self.fft_size, hop_length=self.hop_size, return_complex=True))
        magy = torch.abs(torch.stft(target, n_fft=self.fft_size, hop_length=self.hop_size, return_complex=True))

        logx = torch.log(torch.where(magx <= self.epsilon, torch.Tensor([self.epsilon]).to(output.device), magx))
        logy = torch.log(torch.where(magy <= self.epsilon, torch.Tensor([self.epsilon]).to(output.device), magy))

        return F.l1_loss(magx, magy) + F.l1_loss(logx, logy)


# PreEmph is a class that applies an FIR pre-emphasis filter to the signal, the filter coefficients are in the
# filter_cfs argument, and lp is a flag that also applies a low pass filter
# Only supported for single-channel!
class PreEmph(nn.Module):
    def __init__(self, filter_cfs, low_pass=0):
        super(PreEmph, self).__init__()
        self.epsilon = 0.00001
        self.zPad = len(filter_cfs) - 1

        self.conv_filter = nn.Conv1d(1, 1, 2, bias=False)
        self.conv_filter.weight.data = torch.tensor([[filter_cfs]], requires_grad=False)

        self.low_pass = low_pass
        if self.low_pass:
            self.lp_filter = nn.Conv1d(1, 1, 2, bias=False)
            self.lp_filter.weight.data = torch.tensor([[[0.85, 1]]], requires_grad=False)

    def forward(self, output, target):
        # zero pad the input/target so the filtered signal is the same length
        output = torch.cat((torch.zeros(self.zPad, output.shape[1], 1), output))
        target = torch.cat((torch.zeros(self.zPad, target.shape[1], 1), target))
        # Apply pre-emph filter, permute because the dimension order is different for RNNs and Convs in pytorch...
        output = self.conv_filter(output.permute(1, 2, 0))
        target = self.conv_filter(target.permute(1, 2, 0))

        if self.low_pass:
            output = self.lp_filter(output)
            target = self.lp_filter(target)

        return output.permute(2, 0, 1), target.permute(2, 0, 1)

class LossWrapper(nn.Module):
    def __init__(self, losses, pre_filt=None):
        super(LossWrapper, self).__init__()
        loss_dict = {'ESR': ESRLoss(), 'DC': DCLoss()}
        if pre_filt:
            pre_filt = PreEmph(pre_filt)
            loss_dict['ESRPre'] = lambda output, target: loss_dict['ESR'].forward(*pre_filt(output, target))
        loss_functions = [[loss_dict[key], value] for key, value in losses.items()]

        self.loss_functions = tuple([items[0] for items in loss_functions])
        try:
            self.loss_factors = tuple(torch.Tensor([items[1] for items in loss_functions]))
        except IndexError:
            self.loss_factors = torch.ones(len(self.loss_functions))

    def forward(self, output, target):
        loss = 0
        for i, losses in enumerate(self.loss_functions):
            loss += torch.mul(losses(output, target), self.loss_factors[i])
        return loss


class TrainTrack(dict):
    def __init__(self):
        self.update({'current_epoch': 0, 'training_losses': [], 'validation_losses': [], 'train_av_time': 0.0,
                     'val_av_time': 0.0, 'total_time': 0.0, 'best_val_loss': 1e12, 'test_loss': 0})

    def restore_data(self, training_info):
        self.update(training_info)

    def train_epoch_update(self, loss, ep_st_time, ep_end_time, init_time, current_ep):
        if self['train_av_time']:
            self['train_av_time'] = (self['train_av_time'] + ep_end_time - ep_st_time) / 2
        else:
            self['train_av_time'] = ep_end_time - ep_st_time
        self['training_losses'].append(loss)
        self['current_epoch'] = current_ep
        self['total_time'] += ((init_time + ep_end_time - ep_st_time)/3600)

    def val_epoch_update(self, loss, ep_st_time, ep_end_time):
        if self['val_av_time']:
            self['val_av_time'] = (self['val_av_time'] + ep_end_time - ep_st_time) / 2
        else:
            self['val_av_time'] = ep_end_time - ep_st_time
        self['validation_losses'].append(loss)
        if loss < self['best_val_loss']:
            self['best_val_loss'] = loss
