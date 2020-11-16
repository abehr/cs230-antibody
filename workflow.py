import torch
import keras
import sys
import os
import numpy as np
import pandas as pd
import xlrd # for excel with pandas
import subprocess
import esm_src.esm as esm
from argparse import Namespace
import random
import csv

model_name = 'esm1_t34_670M_UR50S'
model_url = 'https://dl.fbaipublicfiles.com/fair-esm/models/%s.pt' % model_name
model_dir = 'models'
model_fp = os.path.join(model_dir, model_name + '.pt')
data_dir = 'data'
cov1_ab_fp = os.path.join(data_dir, 'cov1-antibody.txt')
foldx_metadata_fp = os.path.join(data_dir, '89ksequences.xlsx')

vocab = esm.constants.proteinseq_toks['toks']

embedding_dir = lambda name: os.path.join(data_dir, name + '_embeddings')
fasta_fp = lambda name: os.path.join(data_dir, name + '.fasta')


# 1-indexed list of indices to allowed to mutate
initial_masks = [31,32,33,47,50,51,52,54,55,57,58,59,60,61,62,99,100,101,102,103,104,271,273,274,275,335,336,337,338,340,341]


all_fastas = ['seq85k', 'subset_seq89k', 'random_generated', 'substitution_generated', 'model_generated']

def run(use_cpu=True):

	print('Load initial SARS-CoV-1 antibody sequence')
	with open(cov1_ab_fp) as f: cov1_ab = f.readline().strip()

	# Load FoldX energy calculations for the 89k sequences
	df = import_energy_metadata()

	# Subset of the 89k seqs, for (test) training downstream model
	compute_embeddings('subset_seq89k')
	subset_seq89k_embeddings = load_seqs_and_embeddings('subset_seq89k', use_cpu, df)


	# Randomly generated mutations
	generate_random_predictions(cov1_ab, initial_masks, 9)
	compute_embeddings('random_generated')
	random_generated_embeddings = load_seqs_and_embeddings('random_generated', use_cpu)

	# Model-generated mutations
	model_generated_embeddings = model_predict_seq(cov1_ab, initial_masks, use_cpu)

	return {
		'subset_seq89k': subset_seq89k_embeddings,
		'random_generated': random_generated_embeddings,
		'model_generated': model_generated_embeddings
	}


def compute_embeddings(name):
	print('Compute embeddings for %s' % name)
	assert os.path.exists(fasta_fp(name)), 'Fasta file for %s does not exist' % name
	assert not os.path.exists(embedding_dir(name)), 'Embeddings for %s already exist' % name

	# Download model manually, otherwise torch method from extract will put it in a cache
	if not os.path.exists(model_fp):
		print('Model does not exist locally - downloading %s' % model_name)
		if not os.path.exists(model_dir): os.mkdir(model_dir)
		subprocess.run(['curl', '-o', model_fp, model_url])

	# This script will automatically use GPU if possible, but will not have any errors if not. 
	subprocess.run(['python3', 'esm_src/extract.py', model_fp, fasta_fp(name), embedding_dir(name), 
		'--repr_layers', '34', '--include', 'mean', 'per_tok'])


def import_energy_metadata():
	# Get FoldX calculations from Excel spreadsheet
	assert os.path.exists(foldx_metadata_fp), 'FoldX data file %s does not exist' % foldx_metadata_fp
	print('Read FoldX data from Excel file')
	
	df = pd.read_excel(foldx_metadata_fp, sheet_name=1) # Sheet2

	# Output FoldX calculations (only) to CSV file for faster future import
	csv_fp = os.path.splitext(foldx_metadata_fp)[0] + '_foldx_only.csv'
	if not os.path.isfile(csv_fp):
		out_df = df[['Antibody_ID','FoldX_Average_Whole_Model_DDG', 'FoldX_Average_Interface_Only_DDG']]
		out_df.to_csv(csv_fp)

	return df


def import_energy_metadata_foldx():
	csv_fp = os.path.splitext(foldx_metadata_fp)[0] + '_foldx_only.csv'
	assert os.path.isfile(csv_fp), 'FoldX CSV file does not exist; you need to run import_energy_metadata() first'

	with open(csv_fp) as f:
		r = csv.reader(f)
		r.__next__() # skip header row
		d = {l[1]: np.array([l[2], l[3]]).astype('float32') for l in r}

	return d
	

def load_energy_metadata_foldx(seqs, foldx_dict):
	return np.stack([foldx_dict[seq] for seq in seqs])



def get_embedding_list(name):
	assert os.path.exists(fasta_fp(name)), 'Fasta file for %s does not exist' % name
	assert os.path.exists(embedding_dir(name)), 'Embeddings for %s do not exist' % name
	return np.array([os.path.splitext(x)[0] for x in os.listdir(embedding_dir(name))])


def load_energy_metadata(seqs, energy_metadata):
	metadata_dict = []
	for label in seqs:
		metadata = energy_metadata.loc[energy_metadata.Antibody_ID==label]
		assert metadata.shape[0] > 0, 'Expected a metadata entry for %s' % label
		metadata = metadata.iloc[0]
		metadata_dict.append([
			metadata.FoldX_Average_Whole_Model_DDG,
			metadata.FoldX_Average_Interface_Only_DDG
			# metadata.Statium
		])

	return np.stack(metadata_dict)


def load_embeddings(name, batch, use_cpu=False):
	assert os.path.exists(embedding_dir(name)), 'Embeddings for %s do not exist' % name
	embeddings = []
	for seq in batch:
		f = os.path.join(embedding_dir(name), seq + '.pt')
		assert os.path.isfile(f), 'Requested embedding file(s) not found'
		if use_cpu or not torch.cuda.is_available():
			data = torch.load(f, map_location=torch.device('cpu'))
		else:
			data = torch.load(f)

		label = data['label']
		token_embeddings = np.delete(data['representations'][34], (0), axis=1)

		embeddings.append(torch.unsqueeze(token_embeddings, 0))

	X = torch.cat(embeddings, dim=0)
	X = torch.flatten(X, start_dim=1, end_dim=-1)
	X = X.numpy()
	X = keras.utils.normalize(X, axis=-1, order=2)
	return X





'''
energy_metadata expects a pandas dataframe (output of import_energy_metadata()).
	If provided, it adds FoldX calculations to the output.
subset is an optional list of sequence IDs. Normally, this function returns all
	of the embeddings found in the 'name' embedding dir. If provided, only return
	this subset. 
'''
def load_seqs_and_embeddings(name, use_cpu, energy_metadata=None, subset=None):
	print('Load seqs and embeddings for %s' % name)
	assert os.path.exists(fasta_fp(name)), 'Fasta file for %s does not exist' % name
	assert os.path.exists(embedding_dir(name)), 'Embeddings for %s do not exist' % name
	if energy_metadata is not None:
		assert type(energy_metadata) == pd.core.frame.DataFrame, 'Unexpected energy metadata type'


	print('Load embeddings from files and combine with metadata')
	embeddings_dict = {}
	for seq in (subset if subset else os.listdir(embedding_dir(name))):
		f = os.path.join(embedding_dir(name), seq + ('.pt' if subset else ''))
		assert os.path.isfile(f), 'Requested embedding file(s) not found'
		if use_cpu or not torch.cuda.is_available():
			data = torch.load(f, map_location=torch.device('cpu'))
		else:
			data = torch.load(f)

		label = data['label']
		token_embeddings = np.delete(data['representations'][34], (0), axis=1)
		# logits = np.delete(data['logits'], (0), axis=1)
		d = {'token_embeddings': token_embeddings}

		if energy_metadata is not None:
			metadata = energy_metadata.loc[energy_metadata.Antibody_ID==label]

			assert metadata.shape[0] > 0, 'Expected a metadata entry for %s' % label
			# TODO: There are some duplicate entries, which should be investigaged. 
			# It does not matter for this case, however.
			# assert metadata.shape[0] < 2, 'Expected only one metadata entry for %s' % label
			metadata = metadata.iloc[0] # ignore duplicate entries

			d['FoldX_Average_Whole_Model_DDG'] = metadata.FoldX_Average_Whole_Model_DDG
			d['FoldX_Average_Interface_Only_DDG'] = metadata.FoldX_Average_Interface_Only_DDG
			d['Statium'] = metadata.Statium

		embeddings_dict[label] = d

	return embeddings_dict


def load_template():
	with open(cov1_ab_fp) as f:
		cov1_ab = f.readline().strip()


def generate_random_predictions(seq, masks, num_seqs):
	name = 'random_generated'
	print('Generate %s predictions' % name)
	assert not os.path.exists(fasta_fp(name)), '%s fasta already exists' % name
	
	with open(fasta_fp(name), 'w') as f:
		for n in range(num_seqs):
			f.write('>%s_%d\n' % (name, n+1))
			f.write('%s\n' % random_gen(seq, masks))


def random_gen(seq, masks):
	return ''.join([(random.choice(vocab) if i+1 in masks else tok) for i,tok in enumerate(seq)])


# Simple unmasking method - just predict all masked tokens at once using softmax
def model_predict_seq(seq, masks, use_cpu):
	name = 'cov2_model_predicted'
	print('Generate %s predictions' % name)

	model, alphabet = load_local_model(use_cpu)
	batch_converter = alphabet.get_batch_converter()
		
	# Note this will also pad any sequence with different length
	labels, strs, tokens = batch_converter([('cov1_ab', seq)])
	apply_mask(tokens, masks)

	with torch.no_grad():
		results = model(tokens, repr_layers=[34])

	tokens, _, logits = parse_model_results(tokens, results)

	softmax_predict_unmask(tokens, logits)

	# TODO: directly compare initial and final tokens. How many are unchanged?

	# Compute embedding for the new predicted sequence
	cov2_predicted_str = tokens2strs(alphabet, tokens)[0]
	labels, strs, tokens = batch_converter([(name, cov2_predicted_str)])

	with torch.no_grad():
		results = model(tokens, repr_layers=[34])

	_, token_embeddings, _ = parse_model_results(tokens, results)

	# return in the same format as load_seqs_and_embeddings
	return {labels[i]: {'token_embeddings': token_embeddings[i]} for i in range(len(labels))}


def parse_model_results(batch_tokens, results):
	tokens = np.delete(batch_tokens, (0), axis=1)
	token_embeddings = np.delete(results["representations"][34], (0), axis=1)
	logits = np.delete(results["logits"], (0), axis=1)

	return tokens, token_embeddings, logits


# tokens is 1-indexed because of BOS token; masks is also 1-indexed.
# TODO: vectorize
def apply_mask(tokens, masks):
	for i in range(len(tokens)):
		for j in masks:
			tokens[i][j] = 33


def tokens2strs(alphabet, batch_tokens):
	return [''.join((alphabet.get_tok(t) for t in tokens)) for tokens in batch_tokens]


def load_local_model(use_cpu):
	# (tweaked from pretrained load model)
	alphabet = esm.Alphabet.from_dict(esm.constants.proteinseq_toks)
	model_data = torch.load(model_fp, map_location=torch.device('cpu'))

	pra = lambda s: ''.join(s.split('decoder_')[1:] if 'decoder' in s else s)
	prs = lambda s: ''.join(s.split('decoder.')[1:] if 'decoder' in s else s)
	model_args = {pra(arg[0]): arg[1] for arg in vars(model_data["args"]).items()}
	model_state = {prs(arg[0]): arg[1] for arg in model_data["model"].items()}

	model = esm.ProteinBertModel(
	  Namespace(**model_args), len(alphabet), padding_idx=alphabet.padding_idx
	)
	model.load_state_dict(model_state)

	return model, alphabet


# Predict a specific token (predict_index) or predict all masked
def softmax_predict_unmask(batch_tokens, logits, predict_index=-1):
	sm = torch.nn.Softmax(dim=1)

	for i in range(len(batch_tokens)):
		masks = predict_index if predict_index > -1 else (batch_tokens[i] == 33)
		softmax_masks = sm(logits[i][masks])

		if softmax_masks.size()[0] > 0:
			# torch.amax returns max
			batch_tokens[i][masks] = torch.argmax(softmax_masks, 1)




if __name__ == '__main__':
	pass
	# run()