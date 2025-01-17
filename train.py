import argparse
import math
import pickle
import shutil
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from distutils.util import strtobool
from pathlib import Path

import dgl
import numpy as np
import torch
import yaml
from dgl.dataloading import GraphDataLoader

import wandb
from model_setup import model_from_config
from analysis.metrics import ModelAnalyzer
from data_processing.crossdocked.dataset import (ProteinLigandDataset,
                                                 get_dataloader)
from models.dynamics import LigRecDynamics
from models.ligand_diffuser import KeypointDiffusion
from models.receptor_encoder import ReceptorEncoder
from models.scheduler import Scheduler
from utils import save_model


def parse_arguments():
    p = argparse.ArgumentParser()

    diff_group = p.add_argument_group('diffusion')
    diff_group.add_argument('--precision', type=float, default=None)
    diff_group.add_argument('--feat_norm_constant', type=float, default=None)
    diff_group.add_argument('--rl_dist_threshold', type=float, default=None, help='distsance threshold for receptor-ligand loss function')

    rec_encoder_group = p.add_argument_group('receptor encoder')
    rec_encoder_group.add_argument('--n_keypoints', type=int, default=None, help="number of keypoints produced by receptor encoder module")
    rec_encoder_group.add_argument('--n_convs_encoder', type=int, default=None, help="number of graph convolutions in receptor encoder")
    rec_encoder_group.add_argument('--keypoint_feats', type=int, default=None, help='number of features for receptor keypoints')
    rec_encoder_group.add_argument('--kp_feat_scale', type=float, default=None, help='scaling value for rec encoder keypoint feature attention')
    rec_encoder_group.add_argument('--use_keypoint_feat_mha', type=bool, default=None)
    rec_encoder_group.add_argument('--feat_mha_heads', type=int, default=None)
    rec_encoder_group.add_argument('--rec_enc_loss_type', type=str, default=None)
    rec_encoder_group.add_argument('--apply_kp_wise_mlp', type=bool, default=None)
    rec_encoder_group.add_argument('--rec_enc_hinge_threshold', type=float, default=None)
    rec_encoder_group.add_argument('--k_closest', type=int, default=None)
    rec_encoder_group.add_argument('--fix_rec_pos', type=int, default=None)

    dynamics_group = p.add_argument_group('dynamics')
    dynamics_group.add_argument('--n_convs_dynamics', type=int, default=None, help='number of graph convolutions in the dynamics model')
    dynamics_group.add_argument('--dynamics_feats', type=int, default=None)
    dynamics_group.add_argument('--h_skip_connections', type=bool, default=None)
    dynamics_group.add_argument('--agg_across_edge_types', type=bool, default=None)
    dynamics_group.add_argument('--dynamics_rec_enc_multiplier', type=int, default=None)
    # dynamics_group.add_argument('--keypoint_k', type=int, default=6, help='K for keypoint -> ligand KNN graph')
    # dynamics_group.add_argument('--ligand_k', type=int, default=8, help='K for ligand -> ligand KNN graph')
    # dynamics_group.add_argument('--use_tanh', type=bool, default=True, help='whether to place tanh activation on coordinate MLP output')

    training_group = p.add_argument_group('training')
    training_group.add_argument('--rl_hinge_loss_weight', type=float, default=None, help='weight applied to receptor-ligand hinge loss')
    training_group.add_argument('--rec_encoder_loss_weight', type=float, default=None, help='relative weight applied to receptor encoder OT loss')
    training_group.add_argument('--lr', type=float, default=None, help='base learning rate')
    training_group.add_argument('--weight_decay', type=float, default=None)
    # training_group.add_argument('--clip_grad', type=bool, default=True, help='whether to clip gradients')
    training_group.add_argument('--clip_value', type=float, default=None, help='max gradient value for clipping')
    # training_group.add_argument('--epochs', type=int, default=1000)
    training_group.add_argument('--batch_size', type=int, default=None)
    # training_group.add_argument('--test_interval', type=float, default=1, help="evaluate on test set every test_interval epochs")
    # training_group.add_argument('--train_metrics_interval', type=float, default=1, help="report training metrics every train_metrics_interval epochs")
    # training_group.add_argument('--test_epochs', type=float, default=2, help='number of epochs to run on test set evaluation')
    # training_group.add_argument('--num_workers', type=int, default=1, help='num_workers argument for pytorch dataloader')
    training_group.add_argument('--warmup_length', type=float, default=None)
    training_group.add_argument('--rec_enc_weight_decay_midpoint', type=float, default=None)
    training_group.add_argument('--rec_enc_weight_decay_scale', type=float, default=None)
    training_group.add_argument('--restart_interval', type=float, default=None)
    training_group.add_argument('--restart_type', type=str, default=None)

    # arguments added in refactor
    rec_encoder_group.add_argument('--kp_rad', type=float, default=None)
    rec_encoder_group.add_argument('--use_sameres_feat', type=int, default=None)
    rec_encoder_group.add_argument('--n_kk_convs', type=int, default=None)
    rec_encoder_group.add_argument('--n_kk_heads', type=int, default=None)
    p.add_argument('--norm', type=int, default=None)
    p.add_argument('--ll_cutoff', type=float, default=None)
    p.add_argument('--rr_cutoff', type=float, default=None)
    p.add_argument('--kk_cutoff', type=float, default=None)
    p.add_argument('--kl_cutoff', type=float, default=None)
    p.add_argument('--use_interface_points', type=int, default=None)
    p.add_argument('--fix_pos', type=int, default=None)
    p.add_argument('--update_kp_feat', type=int, default=None)
    p.add_argument('--ll_k', type=int, default=None)
    p.add_argument('--kl_k', type=int, default=None)


    # args for gvp
    p.add_argument('--dropout', type=float, default=None)
    p.add_argument('--n_vector_channels', type=int, default=None)

    p.add_argument('--max_fake_atom_frac', type=float, default=None)

    p.add_argument('--use_tanh', type=str, default=None)
    p.add_argument('--message_norm', type=str, default=None)

    p.add_argument('--exp_name', type=str, default=None)
    p.add_argument('--architecture', type=str, default=None)
    p.add_argument('--config', type=str, default=None)
    p.add_argument('--resume', default=None)
    args = p.parse_args()

    if args.config is not None and args.resume is not None:
        raise ValueError('only specify a config file or a resume file but not both')

    if args.config is not None:
        config_file = args.config
    elif args.resume is not None:
        config_file = Path(args.resume).parent / 'config.yml'

    with open(config_file, 'r') as f:
        config_dict = yaml.load(f, Loader=yaml.FullLoader)

    if args.architecture is not None:
        config_dict['diffusion']['architecture'] = args.architecture

    architecture = config_dict['diffusion']['architecture'] if 'architecture' in config_dict['diffusion'] else 'egnn'
    if architecture == 'egnn':
        dynamics_key = 'dynamics'
        rec_encoder_key = 'rec_encoder'
    elif architecture == 'gvp':
        dynamics_key = 'dynamics_gvp'
        rec_encoder_key = 'rec_encoder_gvp'
    else:
        raise ValueError(f'invalid architecture: {architecture}')

    if args.resume is not None:
        config_dict['experiment']['name'] = f"{config_dict['experiment']['name']}_resumed"

    # override config file args with command line args
    args_dict = vars(args)

    if args.exp_name is not None:
        config_dict['experiment']['name'] = args.exp_name

    if args.dropout is not None:
        config_dict[rec_encoder_key]['dropout'] = args.dropout
        config_dict[dynamics_key]['dropout'] = args.dropout
    
    if args.use_sameres_feat is not None:
        check_bool_int(args.use_sameres_feat)
        config_dict[rec_encoder_key]['use_sameres_feat'] = bool(args.use_sameres_feat)

    for arg_name in ['n_kk_convs', 'n_kk_heads']:
        if args_dict[arg_name] is not None:
            config_dict['rec_encoder'][arg_name] = args_dict[arg_name]

    if args.kp_rad is not None:
        config_dict[rec_encoder_key]['kp_rad'] = args.kp_rad

    for arg_name in ['ll_k', 'kl_k']:
        if args_dict[arg_name] is not None:
            config_dict[dynamics_key][arg_name] = args_dict[arg_name]

    for etype in ['ll', 'rr', 'kk', 'kl']:
        if args_dict[f'{etype}_cutoff'] is not None:
            config_dict['graph']['graph_cutoffs'][etype] = args_dict[f'{etype}_cutoff']

    if args.norm is not None:
        check_bool_int(args.norm)
        config_dict['rec_encoder']['norm'] = bool(args.norm)
        config_dict['dynamics']['norm'] = bool(args.norm)
    
    if args.use_interface_points is not None:
        check_bool_int(args.use_interface_points)
        config_dict['rec_encoder_loss']['use_interface_points'] = bool(args.use_interface_points)

    if args.fix_pos is not None:
        check_bool_int(args.fix_pos)
        config_dict['rec_encoder']['fix_pos'] = bool(args.fix_pos)
    
    if args.update_kp_feat is not None:
        check_bool_int(args.update_kp_feat)
        config_dict['dynamics']['update_kp_feat'] = bool(args.update_kp_feat)

    
    scheduler_args = ['warmup_length', 
                      'rec_enc_weight_decay_midpoint', 
                      'rec_enc_weight_decay_scale', 
                      'restart_interval', 
                      'restart_type']
    
    for scheduler_arg in scheduler_args:
        if args_dict[scheduler_arg] is not None:
            config_dict['training']['scheduler'][scheduler_arg] = args_dict[scheduler_arg]

    if args.max_fake_atom_frac is not None:
        config_dict['dataset']['max_fake_atom_frac'] = args.max_fake_atom_frac

    if args.use_tanh is not None:

        if args.use_tanh not in ["True", "False"]:
            raise ValueError()

        config_dict['dynamics']['use_tanh'] = strtobool(args.use_tanh)
        config_dict['rec_encoder']['use_tanh'] = strtobool(args.use_tanh)

    if args.batch_size is not None:
        config_dict['training']['batch_size'] = args.batch_size

    if args.precision is not None:
        config_dict['diffusion']['precision'] = args.precision

    if args.feat_norm_constant is not None:
        config_dict['diffusion']['lig_feat_norm_constant'] = args.feat_norm_constant

    if args.rl_dist_threshold is not None:
        config_dict['diffusion']['rl_dist_threshold'] = args.rl_dist_threshold

    if args.fix_rec_pos is not None:
        if args.fix_rec_pos not in [0, 1]:
            raise ValueError
        config_dict['rec_encoder']['fix_pos'] = bool(args.fix_rec_pos)

    if args.n_keypoints is not None:
        config_dict['graph']['n_keypoints'] = args.n_keypoints

    if args.n_convs_encoder is not None:
        if architecture == 'egnn':
            key = 'n_convs'
        elif architecture == 'gvp':
            key = 'n_rr_convs'
        config_dict[rec_encoder_key][key] = args.n_convs_encoder

    if args.message_norm is not None:

        if args.message_norm.isdecimal():
            args.message_norm = float(args.message_norm)

        config_dict[rec_encoder_key]['message_norm'] = args.message_norm
        config_dict[dynamics_key]['message_norm'] = args.message_norm

    # NOTE: this is a design choice: we are only exploring rec_encoder architectures where n_hidden_feats == n_output_feats
    if args.keypoint_feats is not None:
        if architecture == 'egnn':
            config_dict[rec_encoder_key]['out_n_node_feat'] = args.keypoint_feats
            config_dict[rec_encoder_key]['hidden_n_node_feat'] = args.keypoint_feats
        elif architecture == 'gvp':
            config_dict[rec_encoder_key]['out_scalar_size'] = args.keypoint_feats

    if args.k_closest is not None:
        config_dict[rec_encoder_key]['k_closest'] = args.k_closest

    if args.apply_kp_wise_mlp is not None:
        config_dict['rec_encoder']['apply_kp_wise_mlp'] = args.apply_kp_wise_mlp

    if args.kp_feat_scale is not None:
        config_dict['rec_encoder']['kp_feat_scale'] = args.kp_feat_scale

    if args.use_keypoint_feat_mha is not None:
        config_dict['rec_encoder']['use_keypoint_feat_mha'] = args.use_keypoint_feat_mha

    if args.feat_mha_heads is not None:
        config_dict['rec_encoder']['feat_mha_heads'] = args.feat_mha_heads

    if args.rec_enc_loss_type is not None:
        config_dict['rec_encoder_loss']['loss_type'] = args.rec_enc_loss_type

    if args.rec_enc_hinge_threshold is not None:
        config_dict['rec_encoder_loss']['hinge_threshold'] = args.rec_enc_hinge_threshold

    if args.n_convs_dynamics is not None:
        config_dict['dynamics']['n_layers'] = args.n_convs_dynamics

    if args.h_skip_connections is not None:
        config_dict['dynamics']['h_skip_connections'] = args.h_skip_connections

    if args.agg_across_edge_types is not None:
        config_dict['dynamics']['agg_across_edge_types'] = args.agg_across_edge_types

    if args.dynamics_rec_enc_multiplier is not None:
        config_dict['dynamics']['rec_enc_multiplier'] = args.dynamics_rec_enc_multiplier

    if args.dynamics_feats is not None:
        if architecture == 'egnn':
            key = 'hidden_nf'
        elif architecture == 'gvp':
            key = 'n_hidden_scalars'
        config_dict[dynamics_key][key] = args.dynamics_feats

    if args.rl_hinge_loss_weight is not None:
        config_dict['training']['rl_hinge_loss_weight'] = args.rl_hinge_loss_weight

    if args.rec_encoder_loss_weight is not None:
        config_dict['training']['rec_encoder_loss_weight'] = args.rec_encoder_loss_weight

    if args.lr is not None:
        config_dict['training']['learning_rate'] = args.lr

    if args.weight_decay is not None:
        config_dict['training']['weight_decay'] = args.weight_decay

    if args.clip_value is not None:
        config_dict['training']['clip_value'] = args.clip_value

    return args, config_dict

def check_bool_int(val):
    if val not in [0, 1]:
        raise ValueError

@torch.no_grad()
def test_model(model, test_dataloader, args, device):

    # create data structure to record all losses
    losses = defaultdict(list)

    for _ in range(args['training']['test_epochs']):
        for complex_graphs, interface_points in test_dataloader:

            # set data type of atom features
            for ntype in ['lig', 'rec']:
                complex_graphs.nodes[ntype].data['h_0'] = complex_graphs.nodes[ntype].data['h_0'].float()

            complex_graphs = complex_graphs.to(device)
            if args['rec_encoder_loss']['use_interface_points']:
                interface_points = [ arr.to(device) for arr in interface_points ]

            # do forward pass / get losses for this batch
            loss_dict = model(complex_graphs, interface_points)

            # append losses for this batch into lists of all per-batch losses computed so far
            for k,v in loss_dict.items():
                losses[k].append(v.detach().cpu())

            # combine losses into total loss
            total_loss = loss_dict['l2'] + loss_dict['rec_encoder']*args['training']['rec_encoder_loss_weight']

            # add receptor-ligand hinge loss if it is being computed
            if 'rl_hinge' in loss_dict:
                total_loss = total_loss + loss_dict['rl_hinge']*args['training']['rl_hinge_loss_weight']

            losses['total'].append(total_loss.detach().cpu())

    output_losses = { f'{k}_loss': np.mean(v)  for k,v in losses.items() if k != 'rec_encoder'}

    if args['rec_encoder_loss']['loss_type'] == 'optimal_transport':
        rec_encoder_loss_name = 'ot_loss'
    elif args['rec_encoder_loss']['loss_type'] == 'gaussian_repulsion':
        rec_encoder_loss_name = 'repulsion_loss'
    elif args['rec_encoder_loss']['loss_type'] == 'hinge':
        rec_encoder_loss_name = 'rec_hinge_loss'
    elif args['rec_encoder_loss']['loss_type'] == 'none':
        rec_encoder_loss_name = 'no_rec_enc_loss'

    output_losses[rec_encoder_loss_name] = np.mean(losses['rec_encoder'])

    return output_losses

def main():

    # torch.autograd.set_detect_anomaly(True)

    script_args, config = parse_arguments()

    # determine if we are resuming from a previous run
    resume = script_args.resume is not None

    # initialize wandb
    wandb_init_kwargs = config['wandb']['init_kwargs']
    wandb_init_kwargs['name'] = config['experiment']['name']
    wandb.init(config=config, settings=dict(start_method="thread"), **wandb_init_kwargs)

    # if an experiment name was not given in the config file, weights and biases will have assigned a random name
    if config['experiment']['name'] is None and wandb.run is not None: 
        config['experiment']['name'] = wandb.run.name

    print(f"running experiment {config['experiment']['name']}", flush=True)

    # create output directory
    now = datetime.now().strftime('%m%d%H%M%S')
    results_dir = Path(config['experiment']['results_dir'])
    random_id = str(uuid.uuid1())[:4]
    output_dir_name = f"{config['experiment']['name']}_{now}_{random_id}"
    output_dir = results_dir / output_dir_name
    output_dir.mkdir()
    print(f'results are written to this directory: {output_dir}', flush=True)

    # create metrics files and lists to store metrics
    test_metrics_file = output_dir / 'test_metrics.pkl'
    train_metrics_file = output_dir / 'train_metrics.pkl'
    test_metrics = []
    train_metrics = []

    # set random seed
    torch.manual_seed(42)

    # determine device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print(f'{device=}', flush=True)

    # get batch size
    batch_size = config['training']['batch_size']

    # create datasets
    dataset_path = Path(config['dataset']['location']) 
    train_dataset_path = str(dataset_path / 'train.pkl') 
    test_dataset_path = str(dataset_path / 'test.pkl')
    train_dataset = ProteinLigandDataset(name='train', processed_data_file=train_dataset_path, **config['graph'], **config['dataset'])
    test_dataset = ProteinLigandDataset(name='test', processed_data_file=test_dataset_path, **config['graph'], **config['dataset'])

    # compute number of iterations per epoch - necessary for deciding when to do test evaluations/saves/etc. 
    iterations_per_epoch = len(train_dataset) / batch_size

    # create dataloaders
    train_dataloader = get_dataloader(train_dataset, batch_size=batch_size, num_workers=config['training']['num_workers'], shuffle=True, pin_memory=True)
    test_dataloader = get_dataloader(test_dataset, batch_size=batch_size, num_workers=config['training']['num_workers'], pin_memory=True)

    # determine if we are using interface points
    use_interface_points = config['rec_encoder_loss']['use_interface_points']

    # create diffusion model
    model: KeypointDiffusion = model_from_config(config).to(device)
    
    if resume:
        state_file = script_args.resume
        model.load_state_dict(torch.load(state_file))

    # create optimizer
    optimizer = torch.optim.Adam(
        model.parameters(), 
        lr=config["training"]['learning_rate'],
        weight_decay=config["training"]['weight_decay'])


    # create model analyzer
    model_analyzer = ModelAnalyzer(model=model, dataset=test_dataset, device=device)

    # initialize learning rate scheduler
    scheduler_args = config['training']['scheduler']
    scheduler = Scheduler(
        model=model,
        optimizer=optimizer,
        base_lr=config['training']['learning_rate'],
        output_dir=output_dir,
        rec_enc_loss_weight=config['training']['rec_encoder_loss_weight'],
        **scheduler_args
    )

    # watch model if desired
    if config['wandb']['watch_model']:
        wandb.watch(model, **config['wandb']['watch_kwargs'])

    # write the model configuration to the output directory
    new_configfile_loc = output_dir / 'config.yml'
    with open(new_configfile_loc, 'w') as f:
        yaml.dump(config, f)

    # save args to output dir
    arg_fp = output_dir / 'args.pkl'
    with open(arg_fp, 'wb') as f:
        pickle.dump(config, f)
    
    # create empty lists to record per-batch losses
    losses = defaultdict(list)

    if config['rec_encoder_loss']['loss_type'] == 'optimal_transport':
        rec_encoder_loss_name = 'ot_loss'
    elif config['rec_encoder_loss']['loss_type'] == 'gaussian_repulsion':
        rec_encoder_loss_name = 'repulsion_loss'
    elif config['rec_encoder_loss']['loss_type'] == 'hinge':
        rec_encoder_loss_name = 'rec_hinge_loss'
    elif config['rec_encoder_loss']['loss_type'] == 'none':
        rec_encoder_loss_name = 'no_rec_enc_loss'
    
    # create markers for deciding when to evaluate on the test set, report training metrics, save the model
    test_report_marker = 0 # measured in epochs
    train_report_marker = 0 # measured in epochs
    save_marker = 0
    sample_eval_marker = 0

    # record start time for training
    training_start = time.time()

    model.train()
    n_epochs = config['training']['epochs']
    n_epochs_ceil = math.ceil(n_epochs)
    for epoch_idx in range(n_epochs_ceil):

        for iter_idx, iter_data in enumerate(train_dataloader):
            complex_graphs, interface_points = iter_data

            current_epoch = epoch_idx + iter_idx/iterations_per_epoch
            # if current_epoch > 6.56943:
            #     print(f'current epoch: {current_epoch:.5f}', flush=True)

            if current_epoch > n_epochs:
                break

            # update learning rate
            scheduler.step_lr(current_epoch)
            rec_encoder_loss_weight = scheduler.get_rec_enc_weight(current_epoch)

            # move data to the gpu
            complex_graphs = complex_graphs.to(device)
            if use_interface_points:

                # get the number of interface points in every sample
                n_interface_points = [ arr.shape[0] for arr in interface_points ]

                # concatenate interface points into a single array
                interface_points = torch.cat(interface_points, dim=0)

                # move interface points to the gpu
                interface_points = interface_points.to(device)

                # split interface points back out into a list of arrays
                interface_points = torch.split(interface_points, n_interface_points)

            optimizer.zero_grad()
            # TODO: add random translations to the complex positions??

            # encode receptor, add noise to ligand and predict noise
            loss_dict = model(complex_graphs, interface_points)

            # append losses for this batch into lists of all per-batch losses computed so far
            for k,v in loss_dict.items():
                losses[k].append(v.detach().cpu())

            # combine losses
            total_loss = loss_dict['l2']
            if rec_encoder_loss_weight > 0:
                total_loss = total_loss + loss_dict['rec_encoder']*rec_encoder_loss_weight

            if 'rl_hinge' in loss_dict:
                total_loss = total_loss + loss_dict['rl_hinge']*config['training']['rl_hinge_loss_weight']


            total_loss.backward()

            if config['training']['clip_grad']:
                torch.nn.utils.clip_grad_value_(model.parameters(), clip_value=config['training']['clip_value'])
            optimizer.step()

            # save the model if necessary
            if current_epoch - save_marker >= config['training']['save_interval']:
                save_marker = current_epoch # update save marker
                file_name = f'model_epoch_{epoch_idx}_iter_{iter_idx}.pt' # where to save current model
                file_path = output_dir / file_name 
                most_recent_model = output_dir / 'model.pt' # filepath of most recently saved model - note this is always the same path
                save_model(model, file_path)
                save_model(model, most_recent_model)

            # evaluate the quality of sampled molecules, if necessary
            if current_epoch - sample_eval_marker >= config['training']['sample_interval']:

                # reset marker
                sample_eval_marker = current_epoch

                # sample molecules / compute metrics of their quality
                model.eval()
                molecule_quality_metrics = model_analyzer.sample_and_analyze(**config['sampling_config'])
                molecule_quality_metrics['epoch_exact'] = current_epoch
                model.train()

                # print metrics
                print('molecule quality metrics')
                print(*[ f'{k} = {v:.3E}' for k,v in molecule_quality_metrics.items()], sep='\n', flush=True)
                print('\n')

                # log metrics to wandb
                wandb.log(molecule_quality_metrics)

            # test the model if necessary
            if current_epoch - test_report_marker >= config['training']['test_interval'] or current_epoch == 0:
                test_report_marker = current_epoch

                model.eval()
                test_metrics_row = test_model(model, test_dataloader, config, device=device)
                model.train()

                test_metrics_row['epoch_exact'] = current_epoch
                test_metrics_row['epoch'] = epoch_idx
                test_metrics_row['iter'] = iter_idx
                test_metrics_row['time_passed'] = time.time() - training_start 
                test_metrics.append(test_metrics_row)
                with open(test_metrics_file, 'wb') as f:
                    pickle.dump(test_metrics, f)

                print('test metrics')
                print(*[ f'{k} = {v:.3E}' for k,v in test_metrics_row.items()], sep='\n', flush=True)
                print('\n')

                # log test metrics to wandb
                test_metrics_wandb = test_metrics_row.copy()
                for key in list(test_metrics_wandb): # prepend 'train' onto all loss metrics
                    if 'loss' in key:
                        new_key = f'test_{key}'
                        test_metrics_wandb[new_key] = test_metrics_wandb[key]
                        del test_metrics_wandb[key]
                wandb.log(test_metrics_wandb)


            # record train metrics if necessary
            if current_epoch - train_report_marker >= config['training']['train_metrics_interval']:
                train_report_marker = current_epoch

                # TODO: edit this to use the new method of reporting losses from the ligand diffusion model!!
                train_metrics_row = { f'{k}_loss': np.mean(v) for k,v in losses.items() if k != 'rec_encoder' }

                train_metrics_row[rec_encoder_loss_name] = np.mean(losses['rec_encoder'])

                train_metrics_row.update({
                    'epoch': epoch_idx,
                    'epoch_exact': current_epoch,
                    'iter': iter_idx,
                    'time_passed': time.time() - training_start,
                    'rec_enc_loss_weight': rec_encoder_loss_weight,
                    'learning_rate': scheduler.get_lr()
                })

                train_metrics.append(train_metrics_row)
                with open(train_metrics_file, 'wb') as f:
                    pickle.dump(train_metrics, f)

                print('training metrics')
                print(*[ f'{k} = {v:.3E}' for k,v in train_metrics_row.items()], sep='\n', flush=True)
                print('\n')

                # log train metrics to wandb
                train_metrics_wandb = train_metrics_row.copy()
                for key in list(train_metrics_wandb): # prepend 'train' onto all loss metrics
                    if key.split('_')[-1] == 'loss':
                        new_key = f'train_{key}'
                        train_metrics_wandb[new_key] = train_metrics_wandb[key]
                        del train_metrics_wandb[key]
                wandb.log(train_metrics_wandb)

                # reset the record losses to empty dicts
                losses = defaultdict(list)


    # after exiting the training loop, save the final model file
    most_recent_model = output_dir / 'model.pt'
    torch.save(model.state_dict(), str(most_recent_model))


if __name__ == "__main__":
    main()