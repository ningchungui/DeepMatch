# -*- coding:utf-8 -*-
"""
Author:
    Zhe Wang, 734914022@qq.com

Reference:
    [1] Lv, Fuyu, Jin, Taiwei, Yu, Changlong etc. SDM: Sequential Deep Matching Model for Online Large-scale Recommender System[J].
"""

import tensorflow as tf
from deepctr.feature_column import build_input_features, SparseFeat, DenseFeat, get_varlen_pooling_list, \
    VarLenSparseFeat, \
    create_embedding_matrix, embedding_lookup, varlen_embedding_lookup, concat_func
from deepctr.layers.utils import NoMask
from tensorflow.python.keras.layers import Dense, Lambda
from tensorflow.python.keras.models import Model

from ..layers.core import PoolingLayer, SampledSoftmaxLayer, EmbeddingIndex
from ..layers.interaction import UserAttention, SelfMultiHeadAttention, AttentionSequencePoolingLayer
from ..layers.sequence import DynamicMultiRNN
from ..utils import get_item_embedding, l2_normalize


def SDM(user_feature_columns, item_feature_columns, history_feature_list, units=64, rnn_layers=2,
        dropout_rate=0.2, rnn_num_res=1, num_head=4, l2_reg_embedding=1e-6, dnn_activation='tanh',
        temperature=0.05, sampler_config=None, seed=1024):
    """Instantiates the Sequential Deep Matching Model architecture.

    :param user_feature_columns: An iterable containing user's features used by  the model. list of SparseFeat or VarLenSparseFeat.
    :param item_feature_columns: An iterable containing item's features used by  the model. list of SparseFeat or VarLenSparseFeat.
    :param history_feature_list: list,to indicate short and prefer sequence sparse field, such as ['movie_id', 'genres']
    :param units: int, dimension for each output layer
    :param rnn_layers: int, layer number of rnn
    :param dropout_rate: float in [0,1), the probability we will drop out a given DNN coordinate.
    :param rnn_num_res: int. The number of residual layers in rnn layers
    :param num_head: int int, the number of attention head
    :param l2_reg_embedding: float. L2 regularizer strength applied to embedding vector
    :param dnn_activation: Activation function to use in deep net
    :param temperature: float. Scaling factor.
    :param sampler_config: negative sample config.
    :param seed: integer ,to use as random seed.
    :return: A Keras model instance.

    """

    if len(item_feature_columns) > 1:
        raise ValueError("Now SDM only support 1 item feature like item_id")
    item_feature_column = item_feature_columns[0]
    item_feature_name = item_feature_column.name
    item_vocabulary_size = item_feature_columns[0].vocabulary_size

    user_input_dict = build_input_features(user_feature_columns)  # features -> dict(name, Input)
    user_inputs_list = list(user_input_dict.values())

    sparse_feature_columns = list(filter(lambda x: isinstance(x, SparseFeat), user_feature_columns)) if user_feature_columns else []
    dense_feature_columns = list(filter(lambda x: isinstance(x, DenseFeat), user_feature_columns)) if user_feature_columns else []
    if len(dense_feature_columns) != 0:
        raise ValueError("Now SDM don't support dense feature")
    varlen_sparse_feature_columns = list(filter(lambda x: isinstance(x, VarLenSparseFeat), user_feature_columns)) if user_feature_columns else []

    sparse_varlen_feature_columns = []
    prefer_history_columns = []
    short_history_columns = []

    prefer_fc_names = list(map(lambda x: "prefer_" + x, history_feature_list))
    short_fc_names = list(map(lambda x: "short_" + x, history_feature_list))
    for fc in varlen_sparse_feature_columns:
        feature_name = fc.name
        if feature_name in prefer_fc_names:
            prefer_history_columns.append(fc)
        elif feature_name in short_fc_names:
            short_history_columns.append(fc)
        else:
            sparse_varlen_feature_columns.append(fc)

    embedding_matrix_dict = create_embedding_matrix(user_feature_columns + item_feature_columns, l2_reg_embedding, seed=seed)

    item_input_dict = build_input_features(item_feature_columns)  # item_features -> dict(name, Input)
    item_inputs_list = list(item_input_dict.values())

    prefer_emb_list = embedding_lookup(embedding_matrix_dict, user_input_dict, prefer_history_columns,
                                       prefer_fc_names, prefer_fc_names, to_list=True)  # L^u
    short_emb_list = embedding_lookup(embedding_matrix_dict, user_input_dict, short_history_columns,
                                      short_fc_names, short_fc_names, to_list=True)  # S^u
    # dense_value_list = get_dense_input(features, dense_feature_columns)
    user_emb_list = embedding_lookup(embedding_matrix_dict, user_input_dict, sparse_feature_columns, to_list=True)

    sequence_embed_dict = varlen_embedding_lookup(embedding_matrix_dict, user_input_dict, sparse_varlen_feature_columns)
    sequence_embed_list = get_varlen_pooling_list(sequence_embed_dict, user_input_dict, sparse_varlen_feature_columns, to_list=True)
    user_emb_list += sequence_embed_list  # e^u
    # if len(user_emb_list) > 0 or len(dense_value_list) > 0:
    #     user_emb_feature = combined_dnn_input(user_emb_list, dense_value_list)
    user_emb = concat_func(user_emb_list)  # user_emb shape(None, 1, 80)
    user_emb_output = Dense(units, activation=dnn_activation, name="user_emb_output")(user_emb)  # user_emb_output shape(None, 1, 32)

    prefer_sess_length = user_input_dict['prefer_sess_length']  # shape(None,1) dtype:int32
    prefer_att_outputs = []
    for prefer_emb in prefer_emb_list:  # prefer_emb shape(None, 50, 32) dtype:float32
        prefer_attention_output = AttentionSequencePoolingLayer(dropout_rate=0)([user_emb_output, prefer_emb, prefer_sess_length])  # prefer_attention_output(None, 1, 32)
        prefer_att_outputs.append(prefer_attention_output)
    prefer_att_concat = concat_func(prefer_att_outputs)
    prefer_output = Dense(units, activation=dnn_activation, name="prefer_output")(prefer_att_concat)

    short_sess_length = user_input_dict['short_sess_length']
    short_emb_concat = concat_func(short_emb_list)
    short_emb_input = Dense(units, activation=dnn_activation, name="short_emb_input")(short_emb_concat)

    short_rnn_output = DynamicMultiRNN(num_units=units, return_sequence=True, num_layers=rnn_layers, num_residual_layers=rnn_num_res,
                                       dropout_rate=dropout_rate)([short_emb_input, short_sess_length])

    short_att_output = SelfMultiHeadAttention(num_units=units, head_num=num_head, dropout_rate=dropout_rate, future_binding=True,
                                              use_layer_norm=True)([short_rnn_output, short_sess_length])  # [batch_size, time, num_units]

    short_output = UserAttention(num_units=units, activation=dnn_activation, use_res=True, dropout_rate=dropout_rate)([user_emb_output,
                                                                                                                       short_att_output,
                                                                                                                       short_sess_length])

    gate_input = concat_func([prefer_output, short_output, user_emb_output])
    gate = Dense(units, activation='sigmoid')(gate_input)

    gate_output = Lambda(lambda x: tf.multiply(x[0], x[1]) + tf.multiply(1 - x[0], x[2]))([gate, short_output, prefer_output])
    gate_output_reshape = Lambda(lambda x: tf.squeeze(x, 1))(gate_output)
    gate_output_reshape = l2_normalize(gate_output_reshape)

    item_index = EmbeddingIndex(list(range(item_vocabulary_size)))(item_input_dict[item_feature_name])
    item_embedding_matrix = embedding_matrix_dict[item_feature_name]
    item_embedding_weight = NoMask()(item_embedding_matrix(item_index))

    pooling_item_embedding_weight = PoolingLayer()([item_embedding_weight])
    pooling_item_embedding_weight = l2_normalize(pooling_item_embedding_weight)
    output = SampledSoftmaxLayer(sampler_config._asdict(), temperature)([pooling_item_embedding_weight, gate_output_reshape,
                                                                         item_input_dict[item_feature_name]])
    model = Model(inputs=user_inputs_list + item_inputs_list, outputs=output)

    # model.user_input = user_inputs_list
    # model.user_embedding = gate_output_reshape

    model.__setattr__("user_input", user_inputs_list)
    model.__setattr__("user_embedding", gate_output_reshape)

    # model.item_input = item_inputs_list
    # model.item_embedding = get_item_embedding(pooling_item_embedding_weight, item_features[item_feature_name])

    model.__setattr__("item_input", item_inputs_list)
    model.__setattr__("item_embedding", get_item_embedding(pooling_item_embedding_weight, item_input_dict[item_feature_name]))

    return model
