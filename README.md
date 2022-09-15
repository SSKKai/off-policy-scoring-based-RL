### Description
------------
Source code of Improving Feedack Efficiency of Interactive Reinforcement Learning via Gloal Scoring with Adaptive Updating

### Requirements
------------
*   [PyTorch](http://pytorch.org/)
*   [Gym](https://github.com/openai/gym)

Optional Environments:
*   [mujoco-py](https://github.com/openai/mujoco-py)
*   [Metaworld](https://github.com/rlworkgroup/metaworld)
*   [RLBench](https://github.com/stepjam/RLBench)

We use hydra to manage our configs:
```
pip install hydra-core --upgrade
```

Others requiremets are listed in requirements.txt

### Usage

```
usage: python main.py [--config-name CONFIG_NAME]
```

We use `hydra` and `.yaml` file to manage our hypermaraters. We listed the config file in `\config` for different experiments. To run them, just specify the `CONFIG_NAME`. For example: `--config_name mujoco-HalfCheetah-fb250`

If you want to try your own configurations, you can follow these example config and write your own `.yaml` configuration file. We note the meaning of every hyperparameter in the config file.

We use wandb to log our experiments data, if you want to use it, please change the hyperparameters `wand_log` to `true`.

