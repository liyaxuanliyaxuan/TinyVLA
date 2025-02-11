
import pickle

import os
os.environ["TOKENIZERS_PARALLELISM"] = "false"

os.environ['DEVICE'] = "cuda"
os.environ["WANDB_DISABLED"] = "true"

from data_utils.datasets import load_data  # data functions
from data_utils.datasets import compute_dict_mean, set_seed  # helper functions

from aloha_scripts.constants import TASK_CONFIGS
from data_utils.datasets import LlavaPythiaProcess


import IPython
e = IPython.embed
import llava_pythia.llava_pythia_utils as LlavaUtils
from data_utils.processor import *
from llava_pythia.train.llava_pythia_trainer import LLaVAPythiaTrainer
from llava_pythia.model.language_model.pythia.llava_pythia import LlavaPythiaConfig

local_rank = None

#  >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>parameters<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

@dataclass
class ActionHeadArguments:
    multi_step_action: int = 1

@dataclass
class ModelArguments:
    action_head_type: str = field(default="act") # action head type, 'act', 'unet'

    model_name_or_path: Optional[str] = field(default="facebook/opt-125m") # equals to base model path when set load_pretrain=True
    version: Optional[str] = field(default="v0")

    mm_use_im_start_end: bool = field(default=False)
    mm_use_im_patch_token: bool = field(default=True)

    concat: str = field(default="None")
    policy_class: str = field(default="droid_diffusion")


@dataclass
class DataArguments:
    lazy_preprocess: bool = False
    is_multimodal: bool = False
    image_aspect_ratio: str = 'square'
    task_name: str = field(default="stack_cube_2024_6_2")
    skip_mirrored_data: bool = field(default=False)
    chunk_size: int = field(default=50)

@dataclass
class TrainingArguments(transformers.TrainingArguments):
    cache_dir: Optional[str] = field(default=None)
    optim: str = field(default="adamw_torch")
    adam_beta1: float = field(default=0.9)
    adam_beta2: float = field(default=0.98)
    adam_epsilon: float = field(default=1e-7)
    remove_unused_columns: bool = field(default=False)

    freeze_vision_tower: bool = field(default=False)
    freeze_backbone: bool = field(default=False)
    tune_mm_mlp_adapter: bool = field(default=False)

    # logger
    logging_dir: str = field(default='./logs')  # TensorBoard日志的保存目录
    logging_strategy: str = field(default='steps')  # 设置为`steps`表示每几步记录一次日志
    logging_steps: int = field(default=10)

    save_steps: int = field(default=10)  # 每隔多少步保存一次模型
    num_train_epochs: int = field(default=3)
    max_steps: int = field(default=5000)
    seed: int = field(default=0)

    # validate
    do_eval: bool = field(default=True)
    evaluation_strategy: str = field(default="steps")
    eval_steps: int = field(default=200)
    per_device_eval_batch_size: int = field(default=32)

    # pretrain
    load_pretrain: bool = False
    pretrain_image_size : int = 480 # default 270 x 480 and pretrain may be 180 x 320

    dataloader_pin_memory: bool = False
    # lora
    lora_enable: bool = True
    lora_module: str = "vit"
    lora_r: int = 64
    lora_task_type: str = 'CAUSAL_LM'#'FEATURE_EXTRACTION'
    lora_alpha: int = 256
    lora_dropout: float = 0.05
    lora_weight_path: str = ""
    lora_bias: str = "none"
    non_lora_lr: Optional[float] = 3e-5
    group_by_modality_length: bool = field(default=False)

    model_max_length: int = field(
        default=2048,
        metadata={
            "help":
                "Maximum sequence length. Sequences will be right padded (and possibly truncated)."
        },
    )
    double_quant: bool = field(
        default=True,
        metadata={"help": "Compress the quantization statistics through double quantization."}
    )
    quant_type: str = field(
        default="nf4",
        metadata={"help": "Quantization data type to use. Should be one of `fp4` or `nf4`."}
    )
    bits: int = field(
        default=16,
        metadata={"help": "How many bits to use."}
    )


#  <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<parameters>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

def rank0_print(*args):
    if local_rank == 0:
        print(*args)

def parse_pythia():
    global local_rank

    parser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()
    # training_args.logging_dir = '/media/rl/HDD/projects/vlm_diffusion/log'
    # print("##"*50)
    # print(training_args.logging_dir)
    local_rank = training_args.local_rank
    compute_dtype = (torch.float16 if training_args.fp16 else (torch.bfloat16 if training_args.bf16 else torch.float32))

    #     print("##"*50)
    #     print(training_args.logging_dir)

    bnb_model_from_pretrained_args = {}
    if training_args.bits in [4, 8]:
        from transformers import BitsAndBytesConfig
        bnb_model_from_pretrained_args.update(dict(
            device_map={"": training_args.device},
            load_in_4bit=training_args.bits == 4,
            load_in_8bit=training_args.bits == 8,
            quantization_config=BitsAndBytesConfig(
                load_in_4bit=training_args.bits == 4,
                load_in_8bit=training_args.bits == 8,
                llm_int8_skip_modules=["mm_projector"],
                llm_int8_threshold=6.0,
                llm_int8_has_fp16_weight=False,
                bnb_4bit_compute_dtype=compute_dtype,
                bnb_4bit_use_double_quant=training_args.double_quant,
                bnb_4bit_quant_type=training_args.quant_type  # {'fp4', 'nf4'}
            )
        ))

    if "pythia" in model_args.model_name_or_path.lower():
        config = LlavaPythiaConfig.from_pretrained(model_args.model_name_or_path, trust_remote_code=True)
    elif "phi" in model_args.model_name_or_path.lower():
        config = LlavaPhiConfig.from_pretrained(model_args.model_name_or_path, trust_remote_code=True)
    for k,v in asdict(action_head_args).items():
        assert k in config.action_head['action_head'].keys(), f"there is no {k}"
        config.action_head['action_head'][k] = v
    config.concat = asdict(model_args)['concat']

    return model_args, data_args, training_args, config, bnb_model_from_pretrained_args
def train_bc(train_dataset=None, val_dataset=None, model=None, config=None, sampler_params=None, tokenizer=None):

    set_seed(config['training_args'].seed)

    data_collator = DataCollatorForSupervisedDataset(tokenizer=tokenizer)

    data_module = dict(train_dataset=train_dataset,
                       data_collator=data_collator,
                       eval_dataset=val_dataset
                       )
    trainer = LLaVAPythiaTrainer(model=model,
                                 tokenizer=tokenizer,
                                 args=config['training_args'],
                                 sampler_params=sampler_params,
                                 **data_module)

    trainer.train()

    trainer.save_state()

    model.config.use_cache = True

    if config['training_args'].lora_enable:
        state_dict = LlavaUtils.get_peft_state_maybe_zero_3(
            model.named_parameters(), config['training_args'].lora_bias
        )
        non_lora_state_dict = LlavaUtils.get_peft_state_non_lora_maybe_zero_3(
            model.named_parameters(), require_grad_only=False
        )
        if config['training_args'].local_rank == 0 or config['training_args'].local_rank == -1:
            model.config.save_pretrained(config['training_args'].output_dir)
            model.save_pretrained(config['training_args'].output_dir, state_dict=state_dict)
            torch.save(non_lora_state_dict,
                       os.path.join(config['training_args'].output_dir, 'non_lora_trainables.bin'))
    else:
        LlavaUtils.safe_save_model_for_hf_trainer(trainer=trainer,
                                                  output_dir=config['training_args'].output_dir)



def main(config=None, llava_pythia_config=None):
    set_seed(1)
    # command line parameters
    training_args = config['training_args'].__dict__
    # get task parameters
    task_config = TASK_CONFIGS[config['data_args'].task_name]
    dataset_dir = task_config['dataset_dir']
    # num_episodes = task_config['num_episodes']
    episode_len = task_config['episode_len']
    camera_names = task_config['camera_names']
    stats_dir = task_config.get('stats_dir', None)
    sample_weights = task_config.get('sample_weights', None)
    train_ratio = task_config.get('train_ratio', 0.95)
    name_filter = task_config.get('name_filter', lambda n: True)

    config['camera_names'] = camera_names
    config['episode_len'] = episode_len

    if 'pythia' in config['model_args'].model_name_or_path.lower():
        tokenizer = transformers.AutoTokenizer.from_pretrained(
            config['model_args'].model_name_or_path,
            cache_dir=config['training_args'].cache_dir,
            model_max_length=config['training_args'].model_max_length,
            padding_side="right"
        )
        tokenizer.pad_token_id = 1


    model, data_args = LlavaUtils.load_llava_pythia(config=config, llava_pythia_config=llava_pythia_config, rank0_print=rank0_print, tokenizer=tokenizer)

    llava_pythia_process = LlavaPythiaProcess(data_args, tokenizer=tokenizer, language="pick up the bread and put it into the plate.")

    train_dataset, val_dataset, stats, sampler_params = load_data(dataset_dir, name_filter, camera_names, config['training_args'].per_device_train_batch_size,
                                                           config['training_args'].per_device_eval_batch_size, config['data_args'].chunk_size,
                                                           skip_mirrored_data=config['data_args'].skip_mirrored_data,
                                                           config=config,
                                                           policy_class=config['model_args'].policy_class, stats_dir_l=stats_dir,
                                                           sample_weights=sample_weights, train_ratio=train_ratio, return_dataset=True, llava_pythia_process=llava_pythia_process)

    # exit(0)

    best_ckpt_info = train_bc(train_dataset=train_dataset, model=model, val_dataset=val_dataset, config=config, sampler_params=sampler_params, tokenizer=tokenizer)
    # save dataset stats
    stats_path = os.path.join(config['training_args'].output_dir, f'dataset_stats.pkl')
    with open(stats_path, 'wb') as f:
        pickle.dump(stats, f)


if __name__ == '__main__':
    model_args, data_args, training_args, action_head_args, llava_pythia_config, bnb_model_from_pretrained_args = parse_pythia()
    config = {
        'model_args':model_args,
        'data_args':data_args,
        'training_args':training_args,
        'action_head_args':action_head_args,
        'bnb_model_from_pretrained_args':bnb_model_from_pretrained_args
    }
    import json
    config_dict = {k:asdict(v) if not isinstance(v, dict) else v for k,v in config.items()}

    main(config=config, llava_pythia_config=llava_pythia_config)
    pass


