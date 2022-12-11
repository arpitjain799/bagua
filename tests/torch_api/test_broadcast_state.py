import inspect
import itertools
import logging
import os
import pickle
import unittest

import bagua.torch_api as bagua
import torch
import torch.nn as nn
import torch.nn.functional as F

from tests.internal.multi_process_v2 import MultiProcessTestCase, skip_if_lt_x_gpu

logging.getLogger().setLevel(logging.INFO)


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.fc1 = nn.Linear(2, 10, bias=False)
        self.fc2 = nn.Linear(10, 50, bias=True)
        self.fc3 = nn.Linear(50, 4, bias=False)
        self.relu = nn.ReLU()

    def forward(self, x):
        x = self.relu(self.fc1(x))
        x = self.relu(self.fc2(x))
        x = self.fc3(x)
        return F.softmax(x, dim=1)


def create_model_and_optimizer(opt_class, opt_param):
    model = Net().cuda()
    hyper_param = {
        k: v
        for k, v in opt_param.items()
        if k in inspect.signature(opt_class.__init__).parameters
    }
    optimizer = opt_class(model.parameters(), **hyper_param)
    return model, optimizer


def get_optimizer_param_values(optimizer):
    results = []
    state_dict = optimizer.state_dict()
    for group in state_dict["param_groups"]:
        for param_id in group["params"]:
            if param_id not in state_dict["state"]:
                continue
            params = sorted(state_dict["state"][param_id].items())
            for k, v in params:
                results.append(
                    (k, v.clone().detach().cpu().numpy() if torch.is_tensor(v) else v)
                )
    return results


def run_bagua_broadcast(opt_class, opt_hyper_param):

    bagua_model, bagua_optimizer = create_model_and_optimizer(
        opt_class, opt_hyper_param
    )

    print(torch.cuda.current_device())
    for epoch in range(5):
        logging.debug("Training epoch {}".format(epoch))
        for _ in range(10):
            data = torch.randn(4, 2).cuda()
            target = torch.randn(4, 4).cuda()

            bagua_optimizer.zero_grad()
            output = bagua_model(data)
            loss = nn.MSELoss()(output, target)

            loss.backward()
            bagua_optimizer.step()

    from bagua.torch_api.algorithms import gradient_allreduce

    algorithm = gradient_allreduce.GradientAllReduceAlgorithm()
    bagua_model = bagua_model.with_bagua([bagua_optimizer], algorithm)

    model_params = [
        (k, v.clone().detach().cpu().numpy())
        for k, v in sorted(bagua_model.state_dict().items())
    ]
    optimizer_params = get_optimizer_param_values(bagua_optimizer)

    return model_params, optimizer_params


class TestBroadcastModule(MultiProcessTestCase):
    def setUp(self):
        super(TestBroadcastModule, self).setUp()
        self._spawn_processes()

    def tearDown(self):
        super(TestBroadcastModule, self).tearDown()
        try:
            os.remove(self.file_name)
        except OSError:
            pass

    def _check_result(self):
        msg_rank0 = None
        for i, process in enumerate(self.processes):
            _, msg = self.pid_to_pipe[process.pid].recv()

            if i == 0:
                msg_rank0 = msg
            else:
                self.assertEqual(msg, msg_rank0)

    @property
    def world_size(self) -> int:
        return torch.cuda.device_count()

    @skip_if_no_gpu
    def test_broadcast_module(self):
        # Set deterministic
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        torch.manual_seed(self.rank)

        self._init_bagua_distributed()

        optimizers = [
            (optim_class.__name__, optim_class)
            for optim_class in [
                torch.optim.SGD,
                # torch.optim.Adam,
                # torch.optim.Rprop
            ]
        ]

        optimizer_hyper_param = [
            dict(lr=0.2, momentum=0.9, weight_decay=0.1, centered=True),
            # dict(lr=0.2),
        ]

        bcast_params_list = []
        for (opt_name, opt_class), opt_hyper_param in itertools.product(
            optimizers, optimizer_hyper_param
        ):
            model_params, optimizer_params = run_bagua_broadcast(
                opt_class, opt_hyper_param
            )
            bcast_params_list.append(
                {"model": model_params, "optimizer": optimizer_params}
            )

        return bcast_params_list


if __name__ == "__main__":
    unittest.main()
