from random import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Categorical
from copy import deepcopy
from torch.utils.checkpoint import checkpoint
from typing import NamedTuple
import math
import numpy as np

from env.common_func import generate_trans_mat, random_act
from DHJS_models.embedder import DHJS_embedder
from DHJS_models.encoder import TFJSP_Encoder_DHJS
from DHJS_models.decoder_types import TFJSP_Decoder_DHJS_V2, TFJSP_Decoder_DHJS_V3
from DHJS_models.decoder import TFJSP_Decoder_DHJS_Base
from DHJS_models.utils import get_core_adj_list
from DHJS_models.subgraphs import get_core_adj_mat

class TFJSPModel_DHJS(nn.Module):
    '''
    This version improve training speed, where only selected job node, not operation nodes, computes nearest vehicle nodes
    
    '''
    def __init__(self,
                embedding_dim_,
                hidden_dim_,
                problem,
                ope_feat_dim,
                ma_feat_dim,
                veh_feat_dim,
                n_encode_layers=2,
                tanh_clipping=10.,
                mask_inner=True,
                mask_logits=True,
                normalization='batch',
                n_heads=8,
                checkpoint_encoder=False,
                shrink_size=None,
                consd_trans_time_mat=True,
                encoder_version=1,
                decoder_version=1,
                meta_rl=None,
                **model_paras
                ):
        '''
        Input:
            meta_rl: train_paras['meta_rl']
        '''
        super().__init__()
        
        # === configuration ===
        self.embedding_dim = embedding_dim_
        self.hidden_dim = hidden_dim_
        self.n_encode_layers = n_encode_layers
        self.decode_type = "greedy"
        self.temp = 1.0
        self.normalization = normalization
        self.tanh_clipping = tanh_clipping
        self.ope_feat_dim = ope_feat_dim
        self.ma_feat_dim = ma_feat_dim
        self.veh_feat_dim = veh_feat_dim
        self.job_embedding = model_paras['job_centric']
        
        self.all_feat_dim = ope_feat_dim + ma_feat_dim + veh_feat_dim + 1  # where 1: process time feature
        
        self.model_paras = model_paras

        self.mask_inner = mask_inner
        self.mask_logits = mask_logits

        self.problem = problem
        self.n_heads = n_heads
        self.checkpoint_encoder = checkpoint_encoder
        self.shrink_size = shrink_size
        self.proctime_per_ope_max = model_paras["proctime_per_ope_max"]
        self.transtime_btw_ma_max = model_paras["transtime_btw_ma_max"]
        self.device = model_paras['device']
        self.num_core = 3
        
        if meta_rl is not None:
            batch_size = meta_rl['minibatch']
        else:
            batch_size = model_paras['batch_size']
        # True : when encoding, considers transportation time by incorporating it with the processing time
        # False : at encoding, not consider transportation time, and just select a vehicle with minimum transportation time under selected O-M pair
        self.consd_trans_time_mat = consd_trans_time_mat     

        # === embedder ===
        self.embedder = DHJS_embedder(
            embedding_dim_, self.ope_feat_dim, self.ma_feat_dim, self.veh_feat_dim,
            **model_paras
        )
        # === encoder ===
        self.encoder_version = encoder_version
        
        self.encoder = TFJSP_Encoder_DHJS(
            encoder_version, **model_paras
        )
        # === decoder ===
        self.decoder_version = decoder_version
        self.prev_embed = torch.zeros(size=(batch_size, 1, self.embedding_dim))
        if decoder_version == 1:
            decoder_fn = TFJSP_Decoder_DHJS_Base
            self.prev_embed = torch.zeros(size=(batch_size, 1, 3*self.embedding_dim))
        elif decoder_version == 2:
            decoder_fn = TFJSP_Decoder_DHJS_V2
        elif decoder_version == 3:
            decoder_fn = TFJSP_Decoder_DHJS_V3
            self.prev_embed = torch.zeros(size=(batch_size, 1, 3*self.embedding_dim))
        else:
            raise Exception('decoder version error!')
        self.decoder = decoder_fn(**model_paras)
        
        # For each node we compute (glimpse key, glimpse value, logit key) so 2 * embedding_dim
        step_context_dim = self.embedding_dim
        self.project_node_embeddings = nn.Linear(self.embedding_dim, 3 * self.embedding_dim, bias=False)
        self.project_fixed_context = nn.Linear(self.embedding_dim, self.embedding_dim, bias=False)
        self.project_step_context = nn.Linear(step_context_dim, self.embedding_dim, bias=False)
        self.project_prev_context = nn.Linear(self.embedding_dim, self.embedding_dim, bias=False)
        
        
        assert self.embedding_dim % self.n_heads == 0
        # Note n_heads * val_dim == embedding_dim so input to project_out is embedding_dim
        self.project_out = nn.Linear(self.embedding_dim, self.embedding_dim, bias=False)
        
        # === encoder/decoder version related variables ===
        self.batch_core_adj_list = None
        
    
    def init(self, state, dataset=None, loader=None):
        self.batch_size = state.ope_ma_adj_batch.size(0)
        self.num_opes = state.ope_ma_adj_batch.size(1)
        self.num_mas = state.ope_ma_adj_batch.size(2)
        self.num_jobs = state.mask_job_finish_batch.size(1)
        self.num_vehs = state.mask_veh_procing_batch.size(1)
        
        
        if self.encoder_version == 3:
            self.encoder.init()
        elif self.encoder_version == 5:
            oper_adj_batch = state.ope_adj_batch.float()   # [B, n_opes, n_opes]
            proc_time = deepcopy(state.proc_times_batch) # [B, n_opes, n_mas]
            trans_time = deepcopy(state.trans_times_batch)   # [B, n_mas, n_mas]
            _, offload_trans_time, _, _ = \
                generate_trans_mat(trans_time, state, job_embedding=False)    # [B, n_opes or n_jobs, n_mas, n_vehs] |  | 
            
            adj_mat = torch.cat([oper_adj_batch, proc_time, offload_trans_time], dim=-1)    # [B, n_opes, n_opes + n_mas + n_vehs]
            self.batch_core_adj_list = get_core_adj_list(self.num_opes, self.num_mas, self.num_vehs, adj_mat, self.device, spmat_format=False)
        elif self.encoder_version == 6:
            OO_adj_mat = state.ope_adj_batch.float()    # [B, n_opes, n_opes]
            OM_adj_mat = torch.where(state.proc_times_batch>0, 1., 0.)  # [B, n_opes, n_mas]
            OV_adj_mat = torch.ones(size=(self.batch_size, self.num_opes, self.num_vehs))
            adj_mat = torch.cat([OO_adj_mat, OM_adj_mat, OV_adj_mat], dim=-1)   # [B, n_opes, n_nodes]
            self.batch_core_adj_list = get_core_adj_mat(
                adj_mat, self.num_core, self.num_opes, self.num_mas, self.num_vehs,
                self.device, spmat=False
            )
        
        
    
    def act(self, state, baseline=False):
        return self.forward(state, baseline)
    
    def forward(self, state, baseline=False):
        batch_size = state.ope_ma_adj_batch.size(0)
        num_opes = state.ope_ma_adj_batch.size(1)
        num_jobs = state.mask_job_finish_batch.size(1)
        num_mas = state.ope_ma_adj_batch.size(2)
        num_vehs = state.mask_veh_procing_batch.size(1)
        
        ope_step_batch = torch.where(state.ope_step_batch > state.end_ope_biases_batch,
                                     state.end_ope_biases_batch, state.ope_step_batch)  # [B, n_jobs]

        # === embedding ===
        # if encoder_version==3, outputs embedding list: [T, B, N, D_emb]
        # batch_core_adj_list: batch list: [max_k_core, n_nodes, n_nodes]
        embed_feat_ope, embed_feat_ma,\
            embed_feat_veh, norm_proc_trans_time, norm_offload_trans_time, \
            norm_trans_time, oper_adj_batch, _, norm_MVpair_trans_time, norm_onload_trans_time, \
            mask_dyn_ope_ma_adj, mask_ma \
            = self.embedder.embedding(state, self.encoder_version)

        # === encoding ===
        embedded_ope, embedded_ma, embedded_veh = self.encoder(
            embed_feat_ope, embed_feat_ma, embed_feat_veh, 
            norm_proc_trans_time, norm_offload_trans_time, norm_trans_time, oper_adj_batch,
            self.batch_core_adj_list, norm_MVpair_trans_time, norm_onload_trans_time,
            mask_dyn_ope_ma_adj, mask_ma
        )    # [B, n_opes, D_emb] | [B, n_mas, D_emb]
        
        # === decoding ===
        action, log_p = self._get_action_with_decoder(state, embedded_ope, embedded_ma, embedded_veh, baseline=baseline)
        # print(f'action:{action} | log_p:{log_p}')
        
        # action = random_act(state)
        return action, log_p
    
    def _get_action_with_decoder(self, state, embedded_ope, embedded_ma, embedded_veh, baseline):
        '''
        Input:
            state:
            embedding: [B, n_nodes, D_emb]
        Output:
            action: [3, B]
            log_p: [B, 1]
        '''
        batch_size, num_opes, num_mas = state.ope_ma_adj_batch.size()
        num_jobs = state.mask_job_procing_batch.size(1)
        if self.job_embedding:
            num_opes_jobs = num_jobs
        else:
            num_opes_jobs = num_opes
        
        # === get mask ===
        mask, mask_ope_ma = self._get_mask_ope_ma(state) # [B, n_opes, n_mas]
        mask_veh = ~state.mask_veh_procing_batch   # [B, n_vehs]

        # === preprocess decoding ===
        embedding = torch.cat([embedded_ope, embedded_ma, embedded_veh], dim=1) # [B, n_nodes, D_emb]

        self.decoder.set_nodes_kv(embedding)
        self.decoder.set_ope_kv(embedded_ope)
        self.decoder.set_ma_kv(embedded_ma)
        self.decoder.set_veh_kv(embedded_veh)
        
        # === decoder ===
        action, log_p, prev_embed = self.decoder(
            embedding, None, self.prev_embed, state, mask, mask_ope_ma, mask_veh,
            training=self.training, eval_type=self.model_paras['eval_type'], baseline=baseline,
            job_embedding=self.job_embedding
        )  # [B, n_opes]
        self.prev_embed = prev_embed
        
        
        return action, log_p
        
    
    
    def _get_mask_ope_ma(self, state):
        '''
        Output:
            mask: [B, n_jobs, n_mas]
            mask_ope_ma: [B, n_opes, n_mas]
        '''
        batch_idxes = state.batch_idxes
        batch_size, num_opes, num_mas = state.ope_ma_adj_batch.size()
        num_jobs = state.mask_job_procing_batch.size(1)
        
        ope_step_batch = torch.where(state.ope_step_batch > state.end_ope_biases_batch,
                                     state.end_ope_biases_batch, state.ope_step_batch)  # [B, n_jobs]
        opes_appertain_batch = state.opes_appertain_batch   # [B, n_opes]
        # machine mask
        mask_ma = ~state.mask_ma_procing_batch[batch_idxes] # [B, n_mas]
        
        # machine mask for each job
        eligible_proc = state.ope_ma_adj_batch[batch_idxes].gather(1,
                          ope_step_batch[..., None].expand(-1, -1, state.ope_ma_adj_batch.size(-1))[batch_idxes])    # [B, n_jobs, n_mas]
        dummy_shape = torch.zeros(size=(len(batch_idxes), num_jobs, num_mas))
        ma_eligible = ~state.mask_ma_procing_batch[batch_idxes].unsqueeze(1).expand_as(dummy_shape) # [B, n_jobs, n_mas]
        job_eligible = ~(state.mask_job_procing_batch[batch_idxes] +
                         state.mask_job_finish_batch[batch_idxes])[:, :, None].expand_as(dummy_shape)   # [B, n_jobs, n_mas]
        
        eligible = job_eligible & ma_eligible & (eligible_proc == 1)

        if (~(eligible)).all():
            print("No eligible J-M pair!")
            return
        mask = eligible  # [B, n_jobs, n_mas]
        
        # === operation mask ===
        # : masks current ordered operation for each job
        mask_ope_step = torch.full(size=(batch_size, num_opes), dtype=torch.bool, fill_value=False) 
        tmp_batch_idxes = batch_idxes.unsqueeze(-1).repeat(1, num_jobs) # [B, n_jobs]
        mask_ope_step[tmp_batch_idxes, ope_step_batch] = True
        
        # : mask jobs that have no available machine and are processing
        mask_job = torch.where(mask.sum(dim=-1) > torch.zeros(size=(batch_size, num_jobs)), True, False)  # [B, n_jobs]
        mask_ope_by_job = mask_job.gather(1, opes_appertain_batch)
        
        mask_ope = mask_ope_by_job & mask_ope_step  # [B, n_opes]
        
        # === operation-machine mask ===
        mask_ope_padd = mask_ope[:, :, None].expand(-1, -1, num_mas)    # [B, n_opes, n_mas]
        mask_ma_padd = mask_ma[:, None, :].expand(-1, num_opes, -1) # [B, n_opes, n_mas]
        ope_ma_adj = state.ope_ma_adj_batch[batch_idxes]
        mask_ope_ma = mask_ope_padd & mask_ma_padd & (ope_ma_adj==1)  # [B, n_opes, n_mas]
        
        if (~(eligible)).all():
            print("No eligible O-M pair!")
            return
        
        return  mask, mask_ope_ma
    
    def _make_heads(self, v, num_steps=None):
        '''
        Ex) v = glimpse_key_fixed [B, 1, n_opes + n_mas, D_emb] -> [B, 1, n_opes + n_mas, H, D_emb/H] -> [H, B, 1, n_opes + n_mas, D_emb/H]
        '''
        assert num_steps is None or v.size(1) == 1 or v.size(1) == num_steps
        return (
            v.contiguous().view(v.size(0), v.size(1), v.size(2), self.n_heads, -1)
            .expand(v.size(0), v.size(1) if num_steps is None else num_steps, v.size(2), self.n_heads, -1)
            .permute(3, 0, 1, 2, 4)  # (n_heads, batch_size, num_steps, graph_size, head_dim)
        )       
