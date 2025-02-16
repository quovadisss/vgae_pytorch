import torch
import torch.nn.functional as F
from torch.optim import Adam
from sklearn.metrics import roc_auc_score, average_precision_score, f1_score
import scipy.sparse as sp
import numpy as np
import pandas as pd
import os
import time
import pickle
import networkx as nx

from input_data import load_data
from preprocessing import *
import args
import model

# Train on CPU (hide GPU) due to memory constraints
os.environ['CUDA_VISIBLE_DEVICES'] = ""


# Load data
df = pd.read_csv('data/patent.csv').iloc[:, 1:]

# Split data into train and test
tr_df, ts_df = split_train_test(df)

# Create symmetric adjacency matrix
adj, tr_cpc_order = create_adj(tr_df)
G = nx.Graph(adj)
adj = nx.adjacency_matrix(G)

# Load features
with open('data/features.pkl', 'rb') as fr:
    features = pickle.load(fr)
features = sp.csr_matrix(features).tolil()

# Store original adjacency matrix (without diagonal entries) for later
adj_orig = adj
adj_orig = adj_orig - sp.dia_matrix((adj_orig.diagonal()[np.newaxis, :], [0]), shape=adj_orig.shape)
adj_orig.eliminate_zeros()

adj_train, train_edges, val_edges, val_edges_false, test_edges, test_edges_false = mask_test_edges(adj)
adj = adj_train

# Some preprocessing
adj_norm = preprocess_graph(adj)


num_nodes = adj.shape[0]

features = sparse_to_tuple(features.tocoo())
num_features = features[2][1]
features_nonzero = features[1].shape[0]

# Create Model
pos_weight = float(adj.shape[0] * adj.shape[0] - adj.sum()) / adj.sum()
norm = adj.shape[0] * adj.shape[0] / float((adj.shape[0] * adj.shape[0] - adj.sum()) * 2)


adj_label = adj_train + sp.eye(adj_train.shape[0])
adj_label = sparse_to_tuple(adj_label)



adj_norm = torch.sparse.FloatTensor(torch.LongTensor(adj_norm[0].T), 
                            torch.FloatTensor(adj_norm[1]), 
                            torch.Size(adj_norm[2]))
adj_label = torch.sparse.FloatTensor(torch.LongTensor(adj_label[0].T), 
                            torch.FloatTensor(adj_label[1]), 
                            torch.Size(adj_label[2]))
features = torch.sparse.FloatTensor(torch.LongTensor(features[0].T), 
                            torch.FloatTensor(features[1]), 
                            torch.Size(features[2]))

weight_mask = adj_label.to_dense().view(-1) == 1
weight_tensor = torch.ones(weight_mask.size(0)) 
weight_tensor[weight_mask] = pos_weight

# init model and optimizer
model = getattr(model,args.model)(adj_norm)
optimizer = Adam(model.parameters(), lr=args.learning_rate)


def get_scores(edges_pos, edges_neg, adj_rec):

    def sigmoid(x):
        return 1 / (1 + np.exp(-x))

    # Predict on test set of edges
    preds = []
    pos = []
    for e in edges_pos:
        # print(e)
        # print(adj_rec[e[0], e[1]])
        preds.append(sigmoid(adj_rec[e[0], e[1]].item()))
        pos.append(adj_orig[e[0], e[1]])

    preds_neg = []
    neg = []
    for e in edges_neg:

        preds_neg.append(sigmoid(adj_rec[e[0], e[1]].data))
        neg.append(adj_orig[e[0], e[1]])

    preds_all = np.hstack([preds, preds_neg])
    labels_all = np.hstack([np.ones(len(preds)), np.zeros(len(preds_neg))])
    roc_score = roc_auc_score(labels_all, preds_all)
    ap_score = average_precision_score(labels_all, preds_all)

    return roc_score, ap_score

def get_acc(adj_rec, adj_label):
    labels_all = adj_label.to_dense().view(-1).long()
    preds_all = (adj_rec > 0.5).view(-1).long()
    accuracy = (preds_all == labels_all).sum().float() / labels_all.size(0)
    return accuracy

# train model
for epoch in range(args.num_epoch):
    t = time.time()

    A_pred, Z = model(features)
    optimizer.zero_grad()
    loss = log_lik = norm*F.binary_cross_entropy(A_pred.view(-1), adj_label.to_dense().view(-1), weight = weight_tensor)
    if args.model == 'VGAE':
        kl_divergence = 0.5/ A_pred.size(0) * (1 + 2*model.logstd - model.mean**2 - torch.exp(model.logstd)**2).sum(1).mean()
        loss -= kl_divergence

    loss.backward()
    optimizer.step()

    train_acc = get_acc(A_pred,adj_label)

    val_roc, val_ap = get_scores(val_edges, val_edges_false, A_pred)
    print("Epoch:", '%04d' % (epoch + 1), "train_loss=", "{:.5f}".format(loss.item()),
          "train_acc=", "{:.5f}".format(train_acc), "val_roc=", "{:.5f}".format(val_roc),
          "val_ap=", "{:.5f}".format(val_ap),
          "time=", "{:.5f}".format(time.time() - t))


test_roc, test_ap = get_scores(test_edges, test_edges_false, A_pred)
print("End of training!", "test_roc=", "{:.5f}".format(test_roc),
      "test_ap=", "{:.5f}".format(test_ap))


# Create node pairs representation
# Load adj, train/valid network information
with open('data/tr_val_info.pkl', 'rb') as fr:
    tr_val_info = pickle.load(fr)

tr_links = tr_val_info[1]
val_links = tr_val_info[2]

# Create sparse tensors for link features
traina_indices, traina_values, trainb_indices, trainb_values = make_ind_val(tr_links)
vala_indices, vala_values, valb_indices, valb_values = make_ind_val(val_links)

tra = sparse_tensors(traina_indices, traina_values, len(tr_links), adj.shape[0])
trb = sparse_tensors(trainb_indices, trainb_values, len(tr_links), adj.shape[0])
vala = sparse_tensors(vala_indices, vala_values, len(val_links), adj.shape[0])
valb = sparse_tensors(valb_indices, valb_values, len(val_links), adj.shape[0])


def node2edge(node_a, node_b, output, length, dataset):
    def save_operator(array):
        with open('data/Z_{0}_{1}.pkl'.format(args.edge_operator,
                                                   dataset), 'wb') as fw:
            pickle.dump(array, fw)

    mul_a = torch.matmul(node_a, output)
    mul_b = torch.matmul(node_b, output)

    if args.edge_operator == 'cosine':
        sig = m(cosine(mul_a, mul_b).reshape(length, 1))
        save_operator(sig.detach().numpy())
    elif args.edge_operator == 'hadamard':
        save_operator((mul_a * mul_b).detach().numpy())
    elif args.edge_operator == 'average':
        save_operator(((mul_a + mul_b) / 2).detach().numpy())
    elif args.edge_operator == 'weighted-l1':
        save_operator((torch.abs(mul_a - mul_b)).detach().numpy())
    elif args.edge_operator == 'weighted-l2':
        save_operator((torch.square(mul_a - mul_b)).detach().numpy())


node2edge(tra, trb, Z, len(tr_links), 'tr')
node2edge(vala, valb, Z, len(val_links), 'val')

