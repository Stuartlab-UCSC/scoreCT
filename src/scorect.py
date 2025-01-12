#!/usr/bin/env python3

# Lucas Seninge (lseninge)
# Group members: Lucas Seninge
# Last updated: 01-04-2019
# File: scorect.py
# Purpose: Automated scoring of cell types in scRNA-seq.
# Author: Lucas Seninge (lseninge@ucsc.edu)
# Credits for help to: NA

"""
Script for automated cell type assignment in scRNA-seq data analyzed with the Scanpy package.
(https://github.com/theislab/scanpy)

This script contains the functions allowing to automatically assign cell types to louvain clusters infered with Scanpy,
using the gene ranking for each group. The prerequesite is therefore to have pre-analyzed scRNA-seq data as a Scanpy
object with clustering and gene ranking performed.

The method wrangle_ranked_genes() pulls out the results of the gene ranking for each cluster and wrangle it into a table
with associated rank, z-score, p-value for each gene and each cluster. This informative table is also useful for other
purposes.

This table is used to score cell types in louvain clusters using a reference. This reference is ideally a CSV with
curated markers for each cell types of interest. Choice of the table is left to the user so the method can be flexible
to any organ, species and experiment.

The method score_clusters() divide the previously mentioned table into bins of arbitrary size and create a linear
scoring scale (dependent of the number of top ranked genes retained per cluster and the size of the bin) to score each
cell type in the reference table, for each louvain cluster. A dictionary of scores for each cluster is returned.

Finally, the assign_celltypes() method takes the score dictionary and the Anndata object to assign the cell type with
the best score to each cluster. NA is assigned if there is a tie of 2 or more cell types, to avoid bias. The Anndata
object is automatically updated under Anndata.obs['scorect'].

Example usage:
# Import scanpy and read previously analyzed object saved to .h5
import scanpy.api as sc
import celltype_scorer as ct

adata = sc.read('/path/to/file.h5')

# Wrangle results and load reference file for markers/cell types
marker_df = ct.wrangle_ranked_genes(adata)
ref_mark = pd.read_csv('/path/to/file.csv')

# Score cell types
dict_scores = ct.score_clusters(marker_df, nb_marker=100, bin_size=20, ref_marker=ref_mark)
ct.assign_celltypes(adata, dict_score)

# Verify and plot t-SNE
print(adata.obs)
sc.pl.tsne(adata, color='scorect')
"""
"""
Notes on refactoring.

definitions:

   ranked_genes_df == pd.DataFrame (hugo genes X cluster) with sortable values
   ct_marker_dict == dictionary of cell type ids (strs) pointing to sets of hugo gene names (strs)
   cell_type_score == raw marker score float value
   background_genes == A set of background genes used for randomization
   cell_type_pvalue == pvalue calculated from cell_type_score
   ct_scores_df == pd.DataFrame (cluster ids X cell type ids) with marker scores values
   ct_pvalues_df == pd.DataFrame (cluster ids X cell type ids) with pvalue of association as values
   ct_cell_assignment == pd.Series() of cells to cell type
   
functions:
    
    io/

        get_markers_from_db(species, tissue, url)
            output a ct_marker_dict
        
        read_markers_from_file(filename)
            output a ct_marker_dict
        
        get_background_from_server(speices, tissue, url)
            output a background_genes set
            
        read_background_from_file(filename)
            output a background_genes set
        
        wrangle_ranks_from_anndata(anndata.uns['rank_genes_groups'])
            output a ranked_genes_df
        
        read_ranks_from_file(filename)
            output a ranked_genes_df
            
        
    cell_type_annotation/
        
        score_cell_type(ranked_genes_cluster, marker_set)
            output a single marker score (float)
            
        score_cell_types(ranked_genes_cluster: "rank gene Series for a cluster", ct_marker_dict)
            scores a single cluster for all cell types
            output a single row of the ct_scores_df
       
        randomize_ranking(background_genes)
            output a ranked_genes_df
        
        random_score_compare(ct_scores_df, random_ct_scores_df)
            output a binary matrix with 1 if score < random or 0 if score >= random
    
        assign_celltypes(ct_pvalue_df, cluster_assignment, cutoff)
            output a pandas series where index is cell and value is cell type.
            
example of functional composition:
    
    def cell_type_scores(ranked_genes_df, ct_marker_dict):
        return ranked_genes_df.apply(lambda row: score_cell_types(row, ct_marker_dict), axis=1)
        
    def cell_type_pvalues(ranked_genes_df, ct_marker_dict, background_genes, n_samples=100):
        ct_scores_df = cell_type_scores(ranked_genes_df, ct_marker_dict)
        ct_pvalues_df = pd.DataFrame(columns=ct_scores_df.columns, index=ct_scores_df.index).fillna(0)
            
        for sample in n_samples:
            random_rankings = randomize_rankings(background_genes)
            random_scores = cell_type_scores(random_rankings, ct_marker_df)
            ct_pvalues_df += random_score_compare(ct_scores_df, random_scores)
        
        ct_pvalues_df /= float(n_samples)
        
        return ct_pvalues_df

"""
# Import packages
import pandas as pd
import numpy as np
import requests
import itertools
import re
import matplotlib.pyplot as plt


# WRANGLE & PARSE REF #############

def wrangle_ranked_genes(anndata):
    """
    Wrangle results from the ranked_genes_groups function of Scanpy (Wolf et al., 2018) on louvain clusters.

    This function creates a pandas dataframe report of the top N genes used in the ranked_genes_groups search.

    Args:
        anndata (Anndata object): object from Scanpy analysis.

    Return:
        marker_df (pd.dataframe): dataframe with top N ranked genes per clusters with names, z-score and pval.
    """

    # Get number of top ranked genes per groups
    nb_marker = len(anndata.uns['rank_genes_groups']['names'])
    print('Wrangling: Number of markers used in ranked_gene_groups: ', nb_marker)
    print('Wrangling: Groups used for ranking:', anndata.uns['rank_genes_groups']['params']['groupby'])
    # Wrangle results into a table (pandas dataframe)
    top_score = pd.DataFrame(anndata.uns['rank_genes_groups']['scores']).loc[:nb_marker]
    top_adjpval = pd.DataFrame(anndata.uns['rank_genes_groups']['pvals_adj']).loc[:nb_marker]
    top_gene = pd.DataFrame(anndata.uns['rank_genes_groups']['names']).loc[:nb_marker]
    marker_df = pd.DataFrame()
    # Order values
    for i in range(len(top_score.columns)):
        concat = pd.concat([top_score[[str(i)]], top_adjpval[str(i)], top_gene[[str(i)]]], axis=1, ignore_index=True)
        concat['cluster_number'] = i
        col = list(concat.columns)
        col[0], col[1], col[-2] = 'z_score', 'adj_pvals', 'gene'
        concat.columns = col
        marker_df = marker_df.append(concat)

    return marker_df


def _parse_ref(path, species, organ, context=None, comments=False):
    """
    Parses the ref file of specified species into relevant information specified by user.

    This function takes the big reference file for marker/cell types of the specified species and return a formatted
    table of the relevant fields for automated cell type annotation.

    Args:
        path (str): Path to directory with big reference.
        species (str): Specie of interest.
        organ (str): Organ of interest.
        context (str): Context of data. If None, default to 'healthy' for healthy tissue.
        comments (boolean): print comments of retained genes. Default to False.

    Returns:
        ref_df (pandas.df): Parsed reference dataframe.
    """

    if context is None:
        context = 'healthy'
    # Read csv of relevant specied
    specie_df = pd.read_csv(path + species + '.tsv', sep='\t', index_col=False)

    # Get relevant organ and relevant context
    sub_df = specie_df[specie_df['Organ'] == organ]
    sub_df = sub_df[sub_df['Context'] == context]

    # Create new dataframe ref_df from parsed information
    list_ct = sub_df['Cell Type/ Cell State'].unique().tolist()
    dict_marker = {
        ct: [gene for gene in sub_df[sub_df['Cell Type/ Cell State'] == ct]['Gene name(s)'].unique().tolist()]
        for ct in list_ct}

    # If several gene per row, we need to parse commas
    for ct in dict_marker.keys():
        temp_list = []
        for gene in dict_marker[ct]:
            if ',' in gene:
                temp_list.append(gene.split(','))
            else:
                temp_list.append(gene)
        # Slight modification to avoid spelling strings
        dict_marker[ct] = list(
            itertools.chain.from_iterable(itertools.repeat(x, 1) if isinstance(x, str) else x for x in temp_list))

    # Use dict to initialize df. Here order in memory doesn't mess with loading.
    ref_df = pd.DataFrame({ct: pd.Series(genes) for ct, genes in dict_marker.items()})

    # Print comments if needed
    if comments:
        for i, row in sub_df.iterrows():
            print(row['Gene name(s)'], row['Comment'])

    return ref_df


def use_cellmarkerdb(species, tissue):
    """
    Accesses the cellmarker database (http://biocc.hrbmu.edu.cn/CellMarker/index.jsp)
    Paper: Zhang et al., 2019 (https://academic.oup.com/nar/article/47/D1/D721/5115823)

    Args:
        species (str): Query species.
        tissue (str): Query tissue.

    Returns:
        ref_df (pandas.df): Dataframe with celltypes as columns and gene in rows.
    """

    req = requests.get("http://biocc.hrbmu.edu.cn/CellMarker/download/all_cell_markers.txt")
    parsed_file = []
    for chunk in req.iter_lines():
        chunk = chunk.decode("utf-8").split('\t')
        parsed_file.append(chunk)

    marker_df = pd.DataFrame(columns=parsed_file[0], data=parsed_file[1:])

    # Subset for interesting information
    sub_df = marker_df[marker_df['speciesType'] == species]
    sub_df = sub_df[sub_df['tissueType'] == tissue]

    # Parse information to create new reference
    list_ct = sub_df['cellName'].unique().tolist()
    # Use re to remove special characters
    dict_marker = {
        ct: [re.split('\W+', gene) for gene in sub_df[sub_df['cellName'] == ct]['geneSymbol'].unique().tolist()]
        for ct in list_ct}

    # merge nested lists without spelling strings
    for ct in dict_marker.keys():
        dict_marker[ct] = list(
            itertools.chain.from_iterable(itertools.repeat(x, 1) if isinstance(x, str) else x for x in dict_marker[ct]))

    # Use dict to initialize df. Here order in memory doesn't mess with loading.
    ref_df = pd.DataFrame({ct: pd.Series(genes) for ct, genes in dict_marker.items()})

    return ref_df


def _get_genelist(species):
    """
    Accesses list of species' genes from public server. Supported species: human, mouse.

    Args:
        species (str): Name of the species of interest.

    Returns:
        gene_list (list): List of all genes.
    """
    # Convert name to lowercase to access
    species = species.lower()
    response = requests.get('http://public.gi.ucsc.edu/~lseninge/' + species + '_genes.tsv')
    gene_list = []
    lines = response.iter_lines()
    # Skip first line
    next(lines)
    for chunk in lines:
        chunk = chunk.decode("utf-8")
        gene_list.append(chunk)

    return gene_list

# SCORING #############


def score_clusters(anndata, path=None,
                   species='human', organ='brain',
                   context=None, comments=False,
                   user_ref=None, bin_size=20, random_sampling=1000):
    """
    Assign a p-value and a score to each cell type for each cluster in the data.

    More description on usage here.

    Args:
        anndata (Anndata object): object from Scanpy analysis.
        path (str): Path to directory with provided reference (see GitHub).
        species (str): Specie of interest. Default to 'human'.
        organ (str): Organ of interest. Default to 'brain'.
        context (str): Context of data. If None, default to 'healthy' for healthy tissue.
        comments (boolean): print comments of retained genes. Default to False.
        user_ref (pandas.df): If specified, uses a custom reference file provided by user,
        as a dataframe with a list of known markers per curated cell types.
        bin_size (int): size of bins to score.
        random_sampling (int): Number of iterations for re-scoring and stats with random genes. Default to 1000.

    Return:
        anndata (Anndata object): object from Scanpy analysis updated with .uns slot 'scoreCT':
            'pval_dict':
            stat_dict (dict): Dictionary with louvain clusters as keys and a dictionary of cell type:p-val as values.
            (eg: 1:{CT_1: 0.01, CT_2:0.5} ...})
            'score_dict':
            ref_score (dict): Dictionary with louvain clusters as keys and a dictionary of cell type:score as values.
            (eg: 1:{CT_1: 0, CT_2:3} ...})
    """

    # Check if required are present
    if not (not ('louvain' not in anndata.obs) or not ('leiden' not in anndata.obs)) or 'rank_genes_groups' not in anndata.uns:
        return 'Error: No clustering solution OR gene ranking found. Please perform scanpy.tl.louvain or' \
               'scanpy.tl.leiden for clustering, and sc.tl.rank_genes_groups for differential gene expression.'

    # Get number of markers from anndata object
    nb_marker = len(anndata.uns['rank_genes_groups']['names'])
    # Get ranking of genes from DGE in scanpy object
    print('scoreCT: Wrangling gene ranking...')
    ranked_marker = wrangle_ranked_genes(anndata)

    # Use user reference if specified, otherwise get data of specified species/organ/context
    print('scoreCT: Parsing reference...')
    if user_ref is not None:
        ref_marker = user_ref
    else:
        ref_marker = _parse_ref(path=path, species=species, organ=organ, context=context, comments=comments)

    # Call _score_iter() method to get initial scores for actual gene ranking.
    print('scoreCT: Computing scores...')
    ref_score = _score_iter(ranked_marker=ranked_marker,
                            nb_marker=nb_marker,
                            ref_df=ref_marker,
                            bin_size=bin_size)

    # Iterate for K iterations and get number of time scores are superior to initial scores with random genes.
    # Get list of gene to randomize ranking. human by default. human and mouse available.
    gene_list = _get_genelist(species=species)

    # Empty count dict for stats
    count_dict = {clust: {ct: 0 for ct in ref_score[clust]} for clust in ref_score.keys()}

    for i in range(random_sampling):
        # Right now, we randomize on the whole ranked dataframe, with all cluster.
        # Maybe better to randomize cluster by cluster?
        i_dict = _score_iter(randomize_genes(ranked_marker, gene_list),
                             nb_marker=nb_marker,
                             ref_df=ref_marker,
                             bin_size=bin_size)
        # Check for better score in randomized ranking score dict - NAIVE IMPLEMENTATION
        for clust in ref_score.keys():
            for ct in ref_score[clust].keys():
                if i_dict[clust][ct] >= ref_score[clust][ct]:
                    count_dict[clust][ct] += 1

    # Divide by number of iterations
    stat_dict = {clust: {ct: (float(count) / random_sampling) for ct, count in count_dict[clust].items()
                         } for clust in count_dict.keys()}

    # Correct for multiple testing - to debug to avoid 1 * 18 = 18
    # stat_dict = _correct_pval(stat_dict)
    print('scoreCT: Saving results to Anndata object...')
    anndata.uns['scoreCT'] = {'pval_dict':stat_dict, 'score_dict':ref_score,
                              'clustering':anndata.uns['rank_genes_groups']['params']['groupby']}

    return 'DONE'


def _score_iter(ranked_marker, nb_marker, ref_df, bin_size):
    """
    Get scores for gene ranking of clusters given a reference of cell types/markers.

    The gene ranking for cluster i is divided into bins of given size and a score is given for each gene of the
    reference present in the ranking (score is depending of the bin: linearly scaled).

    Args:
        ranked_marker (pandas.df): A dataframe with ranked markers (from wrangle_ranked_genes()).
        nb_marker (int): number of top markers retained per cluster.
        ref_df (pandas.df): Reference dataframe with a cell type per column and a gene per row.
        bin_size (int): size of bins to score.

    Returns:
        dict_scores (dict): Dictionary with louvain clusters as keys and a dictionary of cell type:score as values.
        (eg: 1:{CT_1: 0, CT_2:3} ...})
    """

    # Initialize score dictionary {cluster_1: {cell_type1: X, cell_type2: Y} ...}
    dict_scores = {}
    # Scale scores -- linear
    score_list = range(1, int(nb_marker / bin_size) + 1)
    # Iterate on clusters
    for clust in set(ranked_marker['cluster_number']):
        # Initialize empty dict for given cluster
        dict_scores[clust] = {}
        sub_df = ranked_marker[ranked_marker['cluster_number'] == clust]
        # Get individual bins
        for k in range(int(nb_marker / bin_size)):
            bin_df = sub_df[(k * bin_size):bin_size + (k * bin_size)]
            # Use ref to score
            # We assume format column = cell types, each row is a different marker
            for cell_type in list(ref_df):
                # Get length of intersection between ref marker and bin , multiply by score of the associated bin
                # score_list[-(1+k)] because k can be 0 for bin purposes
                score_i = score_list[-(1 + k)] * len(set(ref_df[cell_type]).intersection(set(bin_df['gene'])))
                # Add score to existing score or create key
                if cell_type in dict_scores[clust]:
                    dict_scores[clust][cell_type] += score_i
                else:
                    dict_scores[clust][cell_type] = score_i

    return dict_scores


# STATS #############

def randomize_genes(marker_df, gene_list):
    """
    Replaces genes in original data by random genes for rescoring.

    TO ADD: MOUSE GENES.

    Args:
        marker_df (pandas.df): gene ranking for louvain clusters in original data.
        gene_list (list): List of all possible *human* genes.

    Returns:
        copy_df (pandas.df): randomized gene ranking for louvain clusters.
    """

    copy_df = marker_df.copy()
    # add code here to process cluster by cluster instead of the whole df in one time
    rd_pick = np.random.choice(gene_list, len(copy_df['gene']))
    copy_df['gene'] = rd_pick
    return copy_df


def _correct_pval(dict_scores):
    """
    Correct p-values for multiple testing.

    Args:
        dict_scores (dict): dictionary of [scores,p-val] per cluster/cell_types.

    Return:
        dict_scores (dict): input with corrected p-value.
    """

    n_test = len(dict_scores.keys())
    for clust in dict_scores.keys():
        for ct in dict_scores[clust].keys():
            # Multiply by number of cell types
            dict_scores[clust][ct] *= n_test

    return dict_scores


# SUMMARY #############


def pval_plot(anndata, clusters):
    """
    Plot of p-values for given cluster.
    Args:
        anndata (Anndata object). Scanpy object with score dictionaries present.
        clusters (int or list of int): Number of the louvain cluster to check.

    """
    import seaborn as sns

    # If only one cluster is input as int, convert to list
    if type(clusters) == int:
        clusters = list(clusters)
    if 'scoreCT' not in anndata.uns:
        return 'scoreCT results not found. Please perform the scoreCT analysis function first.'

    pval_thrsh = anndata.uns['scoreCT']['pval_thrsh']

    for cluster in clusters:
        # Convert to int if user use str
        cluster = int(cluster)
        # Sort by value
        data = pd.DataFrame.from_dict(anndata.uns['scoreCT']['pval_dict'][cluster], orient='index')
        data = data.sort_values(by=0)
        lists = sorted(anndata.uns['scoreCT']['pval_dict'][cluster].items(), key=lambda kv: kv[1])
        x, y = zip(*lists)
        # Plot with pval threshold
        sns.barplot(x=data.index, y=0, data=data, palette='Blues_d')
        plt.axhline(y=pval_thrsh, color='r', label='Threshold')
        plt.legend()
        plt.ylabel('P-value')
        plt.xticks(rotation=90)
        plt.ylim(0, 2*pval_thrsh)
        plt.title('P-value plot for cluster ' + str(cluster))
        plt.show()


# ASSIGN #############


def assign_celltypes(anndata, pval_thrsh=0.1):
    """
    Given a dictionary of cell type scoring for each cluster, assign cell type with max. score
    to given cluster.

    Args:
        anndata (AnnData object): Scanpy object with analyzed data (clustered cells).
        pval_thrsh (int): p-value threshold for NA values.

    Returns:
        anndata (AnnData object): Scanpy object with assigned cell types.
    """

    # Get clustering method used in ranked_genes_groups
    clust_method = anndata.uns['scoreCT']['clustering']
    # Initialize new metadata column in Anndata object
    anndata.obs['scorect'] = ''
    # Iterate on clusters in dict_stats
    for cluster in anndata.uns['scoreCT']['pval_dict'].keys():
        # Get cell type with lowest pval
        # Add pval threshold. Default to pval=0.1
        min_value = min(anndata.uns['scoreCT']['pval_dict'][cluster].values())
        if min_value > pval_thrsh:
            assign_type = 'NA'
        elif len({key for key, value in anndata.uns['scoreCT']['pval_dict'][cluster].items() if value == min_value}) > 1:
            # If ties, get best score
            # Add ties here too ?
            assign_type = max(anndata.uns['scoreCT']['score_dict'][cluster], key=anndata.uns['scoreCT']['score_dict'][cluster].get)
        else:
            assign_type = min(anndata.uns['scoreCT']['pval_dict'][cluster], key=anndata.uns['scoreCT']['pval_dict'][cluster].get)
        # Update
        anndata.obs.loc[anndata.obs[clust_method] == str(cluster), 'scorect'] = assign_type
        anndata.uns['scoreCT']['pval_thrsh'] = pval_thrsh

    return "Cell types assigned in Anndata.obs['scorect']"
