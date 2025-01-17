import dill as pickle
import numpy as np
import pandas as pd

import os
import warnings
import string
import quail

# noinspection PyProtectedMember
from pathos.multiprocessing import ProcessingPool as Pool
from multiprocessing import cpu_count
from tqdm import tqdm
from sklearn.decomposition import IncrementalPCA as PCA
from scipy import stats
from copy import deepcopy

from dataloader import fetch_data, get_listgroups, datadir, feature_groupings

N_ITER = 500

results_file = f'analyzed_{N_ITER}_iter.pkl'

random = ['feature rich', 'reduced (early)', 'reduced (late)', 'reduced']
adaptive = ['adaptive']
non_adaptive_exclude_random = ['category', 'size', 'length', 'first letter', 'color', 'location']


def apply(egg, analysis, listgroup=None, **kwargs):
    warnings.simplefilter('ignore')
    return egg.analyze(analysis, listgroup=list(range(16)), parallel=False, **kwargs)


def analyze_data(analyses=['fingerprint', 'pfr', 'lagcrp', 'spc', 'accuracy'], data=None, listgroups=None, savefile=None):
    if savefile is not None:
        savefile = os.path.join(datadir, savefile)

        if os.path.exists(savefile):
            with open(savefile, 'rb') as f:
                results, analyses, listgroups = pickle.load(f)
                return results, analyses, listgroups
    
    if data is None:
        data = fetch_data()
    
    if listgroups is None:
        listgroups = get_listgroups(data)

    scratch_dir = os.path.join(datadir, 'scratch')
    if not os.path.exists(scratch_dir):
        os.makedirs(scratch_dir)

    results = {}

    def apply_wrapper(args):
        a, x, d, tmpfile, kwargs = args
        tmpfile = f'{x}-{tmpfile}'
        print(f'starting {a} analysis for condition {x}...')

        if os.path.exists(os.path.join(scratch_dir, tmpfile)):
            with open(os.path.join(scratch_dir, tmpfile), 'rb') as f:
                r = pickle.load(f)
        else:
            r = apply(d, a, **kwargs)
            with open(os.path.join(scratch_dir, tmpfile), 'wb') as f:
                pickle.dump(r, f)

        results[a][x] = r
        print(f'finished {a} analysis for condition {x}')

    print('basic analyses...')
    for a in tqdm(analyses):
        kwargs = {}
        results[a] = {}

        if a == 'fingerprint':
            kwargs['permute'] = True
            kwargs['n_perms'] = N_ITER
            tmpfile = f'{a}-{kwargs["permute"]}-{kwargs["n_perms"]}.pkl'
        else:
            if a == 'pfr':
                kwargs['position'] = 0
            tmpfile = f'{a}.pkl'

        # save out temp files
        # if a not in ['pfr', 'pnr']:
        #     for x, d in data.items():
        #         apply_wrapper()
        #     with Pool(min([cpu_count(), len(data)])) as p:
        #         p.map(apply_wrapper, [[a, x, d, tmpfile, kwargs] for x, d in data.items()])
        # else:
        [apply_wrapper([a, x, d, tmpfile, kwargs]) for x, d in data.items()]
        
        # load in temp files and update results
        for x in data.keys():
            next_tmpfile = f'{x}-{tmpfile}'
            with open(os.path.join(scratch_dir, next_tmpfile), 'rb') as f:
                results[a][x] = pickle.load(f)

    print('pnr analyses...')
    pnr_results = {}
    for i in tqdm(range(16)):
        pnr_results[i] = {x: apply(d, 'pnr', position=i) for x, d in data.items()}

    results['pnr'] = pnr_results

    if savefile is not None:
        with open(savefile, 'wb') as f:
            pickle.dump([results, analyses, listgroups], f)

    return results, analyses, listgroups


# update PFR and SPC curves to use 1-indexing instead of 0-indexing, per reviewer request
def increment_presentation_positions(r):
    if type(r) is dict:
        return {k + 1 if type(k) is int else k: increment_presentation_positions(v) for k, v in r.items()}
    
    x = r.get_data().copy()
    if type(x.columns) is pd.RangeIndex:
        x.columns = pd.RangeIndex(start=x.columns.start + 1, stop=x.columns.stop + 1, step=x.columns.step)
    
    r.data = x
    return r


results, analyses, listgroups = analyze_data(savefile=results_file)
results = {k: increment_presentation_positions(v) if k in ['pfr', 'spc', 'pnr'] else v for k, v in results.items()}


# hack for recovering fingerprint features (feature tags are lost in the pickling process)
def recover_fingerprint_features(order_file, results):
    if os.path.exists(order_file):
        with open(order_file, 'rb') as f:
            orders = pickle.load(f)
    else:
        data = fetch_data()
        fingerprints = {x: d.analyze('fingerprint') for x, d in data.items()}
        orders = {k: x.data.columns.tolist() for k, x in fingerprints.items()}

        with open(order_file, 'wb') as f:
            pickle.dump(orders, f)

    for k, f in results['fingerprint'].items():
        f.data.columns = orders[k]
    
    return orders


order_file = os.path.join(datadir, 'scratch', 'feature_order.pkl')
orders = recover_fingerprint_features(order_file, results)


def organize_by_listgroup(x, groups):
    if type(x) is dict:        
        return {k: organize_by_listgroup(v, groups[k]) if k in groups else organize_by_listgroup(v, groups) for k, v in x.items()}
    
    subjs = x.data.index.levels[0].values
    lists = x.data.index.levels[1].values

    if all([type(g) is list for g in groups]):
        listgroups = {s: g for s, g in enumerate(groups)}
    else:
        listgroups = {s: groups for s in np.unique(subjs)}
    
    data = x.data.reset_index()
    data['listgroup'] = data.apply(lambda r: listgroups[int(r['Subject'])][int(r['List'])], axis=1)
    data = data.groupby(['listgroup', 'Subject']).mean().reset_index()
    data = data.drop('List', axis=1).rename({'listgroup': 'List'}, axis=1).set_index(['Subject', 'List'])

    return quail.FriedEgg(data=data, analysis=x.analysis, list_length=x.list_length, n_lists=x.n_lists, n_subjects=x.n_subjects,
                          position=x.position)


results_by_list = results  # per-list results
results = {a: organize_by_listgroup(results_by_list[a], listgroups) for a in results_by_list.keys()}  # averaged within each listgroup

def select_conds(results, conds='all'):
    return {k: v for k, v in results.items() if conds == 'all' or k in conds}

def select_lists(fried, lists='all'):
    if type(fried) is dict:
        return {k: select_lists(v, lists=lists) for k, v in fried.items()}

    x_data = fried.data.copy()
    if lists == 'all':
        x_data.index = pd.MultiIndex.from_tuples([(i, 'Average') for i, _ in fried.data.index], names=fried.data.index.names)
    elif type(lists) is str:
        x_data = x_data.query(f'List == "{lists}"')
    else:
        x_data = x_data.query(f'List in @lists')

    return quail.FriedEgg(data=x_data.copy(), analysis=fried.analysis, list_length=fried.list_length,
                          n_lists=fried.n_lists, n_subjects=fried.n_subjects, position=fried.position)


def filter(x, include_conds='all', include_lists='all'):
    x = {k: select_lists(v, lists=include_lists) for k, v in select_conds(x, conds=include_conds).items()}
    include_conds = list(x.keys())
    include_conds.sort()

    include_keys = []
    for k, v in x.items():
        include_keys.extend(np.unique([i for _, i in v.data.index]).tolist())
    include_keys = np.unique(include_keys).tolist()

    return x, include_conds, include_keys


def reorder_df(df, column, order):
    new_dfs = []
    for val in order:
        new_dfs.append(df.query(f'{column} == "{val}"'))
    
    return pd.concat(new_dfs, axis=0)


def rename_features(x):
    rename = {'pos': 'location', 'first_letter': 'first letter', 'firstLetter': 'first letter', 'wordLength': 'length'}
    df = x.data.rename(rename, axis=1)
    return df[sort_by_grouping(df.columns.values.tolist(), feature_groupings)]


def egg_diff(a, b):
    if type(a) is dict:
        results = {}
        for k in a.keys():
            results[k] = egg_diff(a[k], b[k])
        return results
    
    for i in ['analysis', 'list_length', 'n_lists', 'n_subjects', 'position']:
        assert getattr(a, i) == getattr(b, i), ValueError('Incompatable eggs; cannot take difference')
    assert np.all([i == j for i, j in zip(a.data.shape, b.data.shape)]), ValueError('Incompatable eggs; cannot take difference')
    
    #idx = pd.MultiIndex.from_tuples([(i, 'Difference') for i, _ in a.data.index], names=a.data.index.names)
    diffs = pd.DataFrame(index=a.data.index, data=a.data.values - b.data.values, columns=a.data.columns)  # Hack: name "differences" using the reference item's name so that coloring works correctly

    return quail.FriedEgg(data=diffs, analysis=a.analysis, list_length=a.list_length,
                          n_lists=a.n_lists, n_subjects=a.n_subjects, position=a.position)
    

def get_diffs(x, contrast={'Late': 'Early'}, include_conds='all' ):
    include_lists = []
    for k, v in contrast.items():
        include_lists.extend([k, v])
    include_lists = np.unique(include_lists).tolist()
        
    x, include_conds, include_lists = filter(x, include_conds, include_lists)
    
    diffs = {}
    for k, v in contrast.items():
        diffs[f'{k} - {v}'] = egg_diff(select_lists(x, k), select_lists(x, v))

    return diffs


def stack_diffs(diffs, include_conds='all'):
    if include_conds == 'all':
        include_conds = []
        for k1, v1 in diffs.items():
            for k2, _ in v1.items():
                include_conds.append(k2)
        include_conds = np.unique(include_conds).tolist()
    elif type(include_conds) is str:
        include_conds = [include_conds]    

    template = diffs
    while type(template) is not quail.FriedEgg:
        template = template[list(template.keys())[0]]
    
    analysis = template.analysis
    list_length = template.list_length
    n_lists = template.n_lists
    n_subjects = template.n_subjects
    position = template.position

    results = {}
    for c in include_conds:
        results[c] = quail.FriedEgg(pd.concat([x[c].data for x in diffs.values()], axis=0),
            analysis=analysis, list_length=list_length, n_lists=n_lists,
            n_subjects=n_subjects, position=position)
    return results


def pnr_matrix(pnr_results, include_conds='all', include_lists='all'):
    positions = range(1, 17)

    if type(include_conds) is str:
        include_conds = [include_conds]
    
    if type(include_lists) is str:
        include_lists = [include_lists]

    conds = [c for c in pnr_results[positions[0]].keys() if 'all' in include_conds or c in include_conds]

    matrices = {}
    for c in conds:
        matrices[c] = {}
        
        x = pd.concat([pnr_results[p][c].data.groupby('List').mean() for p in positions], axis=0)
        lists = np.unique(x.index.values).tolist()

        for ls in lists:
            if 'all' in include_lists or ls in include_lists:
                matrices[c][ls] = x.query(f'List == "{ls}"').reset_index().drop('List', axis=1)

    return matrices


def mini_filter(x, include_conds='all', include_lists='all'):
    conds = []
    lists = []

    if type(include_conds) is str and include_conds != 'all':
        include_conds = [include_conds]

    if type(include_lists) is str and include_lists != 'all':
        include_lists = [include_lists]
    


    results = {}
    for k1 in x.keys():
        if not (include_conds == 'all' or k1 in include_conds):
            continue
        
        results[k1] = {}
        conds.append(k1)
        
        for k2, v in x[k1].items():
            if not (include_lists == 'all' or k2 in include_lists):
                continue

            results[k1][k2] = x[k1][k2]
            lists.append(k2)
    
    return results, np.unique(conds).tolist(), np.unique(lists).tolist()


def sort_by_grouping(vals, groupings):
    sorted_vals = []
    for category, exemplars in groupings.items():
        sorted_vals.extend([x for x in exemplars if x in vals])
    
    missing = [v for v in vals if v not in sorted_vals]
    sorted_vals.extend(missing)
    
    return sorted_vals


def accuracy2df(accuracy):
    columns = accuracy.keys()
    df = pd.concat([v.data for v in accuracy.values()], axis=1)
    df.columns = columns
    return df.reset_index().melt(id_vars=['Subject', 'List'], var_name='Condition', value_name='Accuracy')


def adaptive_listnum2cond(x):
    return listgroups['adaptive'][int(x['Subject'])][int(x['List'])]


def multicaps(x):
    if type(x) is str:
        return x.capitalize()
    elif hasattr(x, '__iter__'):
        return [multicaps(i) for i in x]


def multilower(x):
    if type(x) is str:
        return x.lower()
    elif hasattr(x, '__iter__'):
        return [multilower(i) for i in x]


def clustering_matrices(fingerprints, include_conds='all', include_lists='all'):

    if type(include_conds) is str:
        include_conds = [include_conds]
    
    if type(include_lists) is str:
        include_lists = [include_lists]

    conds = [c for c in fingerprints.keys() if 'all' in include_conds or c in include_conds]

    matrices = {}
    for c in conds:
        matrices[c] = {}

        x = rename_features(fingerprints[c])
        x = x[sort_by_grouping(x.columns, feature_groupings)]
        x.columns = multicaps(x.columns)
        
        lists = np.unique([i[1] for i in x.index]).tolist()

        for ls in lists:
            if 'all' in include_lists or ls in include_lists:
                matrices[c][ls] = x.query(f'List == "{ls}"').corr()

    return matrices


def average_by_cond(x, include_conds='all', include_lists='all'):
    def average_helper(x, cond):
        return pd.DataFrame(rename_features(x[cond]).mean(axis=0)).rename({0: cond}, axis=1).T

    x, conds, _ = filter(x, include_conds, include_lists)
    return pd.concat([average_helper(x, c) for c in conds], axis=0)


def fingerprint2temporal(f):
    return quail.FriedEgg(data=pd.DataFrame(f.data['temporal']).rename({'temporal': 0}, axis=1),  # this weirdness is needed to put temporal clustering data in the same format as the other eggs
                          analysis=f.analysis, list_length=f.list_length,
                          n_lists=f.n_lists, n_subjects=f.n_subjects, position=f.position)


def trajectorize(x, n_dims=2, model=PCA, average=False):
    data = {c: rename_features(f) for c, f in x.items()}

    if average:
        data = {c: d.groupby('List').mean() for c, d in data.items()}

    m = model(n_components=n_dims)
    m.fit(pd.concat([j for i, j in data.items()], axis=0))

    return {c: pd.DataFrame(m.transform(x), index=x.index) for c, x in data.items()}


def get_dists(fingerprints, ref=0):
    if type(fingerprints) is dict:
        return {c: get_dists(x.data) for c, x in fingerprints.items()}
    elif type(fingerprints) is pd.DataFrame:
        by_subj = []
        subjs = fingerprints.index.get_level_values('Subject').unique().tolist()

        for s in subjs:
            by_subj.append(get_dists(fingerprints.query('Subject == @s').values, ref=ref))
        
        df = pd.DataFrame(pd.concat(by_subj, axis=0, ignore_index=True))
        df['Subject'] = subjs
        return df.set_index('Subject')

    dists = []
    for i in range(fingerprints.shape[0]):
        if i == 0 or str(ref).isnumeric():
            dists.append(np.linalg.norm(fingerprints[0, :] - fingerprints[i, :]))
        elif ref == 'mean':
            dists.append(np.linalg.norm(fingerprints[:i, :].mean(axis=0) - fingerprints[i, :]))
    return pd.DataFrame(dists).T


discrete_features = ['category', 'size']
continuous_features = ['color', 'location', 'length', 'first letter']
features = discrete_features + continuous_features


def field2feature(x):
    rename = {'firstLetter': 'first letter', 'first_letter': 'first letter', 'wordLength': 'length', 'pos': 'location'}
    if type(x) is list:
        return [field2feature(y) for y in x]
    
    if x in rename:
        return rename[x]
    else:
        return x


def rename_dict(d):
    return {field2feature(k): v for k, v in d.items()}


def feature_dists(y1, y2, focus=None):    
    if focus is None or focus in continuous_features:
        return [np.linalg.norm(np.array(a) - np.array(b)) for a, b in zip(y1.values, y2.values)]
    else:
        return [int(a != b) for a, b in zip(y1.values, y2.values)]


def get_event_boundaries(data, focus=None, n_stddev=2):    
    if type(data) is dict:
        x = {}
        for k in data.keys():
            try:
                x[k] = get_event_boundaries(data[k], n_stddev=n_stddev)
            except:
                print(f'problem with {k} in get_event_boundaries')
        return x
    elif type(data) is quail.Egg:
        x = data.get_pres_features().applymap(lambda v: rename_dict(v))
        return {f: get_event_boundaries(x, focus=f, n_stddev=n_stddev) for f in features}
    elif type(data) is pd.DataFrame:
        if focus != 'first letter':
            x = data.applymap(lambda v: v[focus])
        else:        
            x = data.applymap(lambda v: [string.ascii_uppercase.index(v[focus].decode())] if type(v[focus]) is np.bytes_ else [string.ascii_uppercase.index(v[focus])])
        dists = pd.DataFrame(index=x.index, columns=x.columns, data=np.zeros(x.shape))

        for i in range(x.shape[1] - 1):
            y1 = x[i]
            y2 = x[i + 1]

            dists[i + 1] = feature_dists(y1, y2, focus=focus)
        
        if np.char.isnumeric(str(n_stddev)):
            if focus in discrete_features:
                return dists.astype(int)
            else:
                # compute a threshold (add an n_stddev param to function definition)
                # binarize the distances as being above vs. below the threshold
            
                thresh = np.mean(dists.values) + n_stddev * np.std(dists.values)
                return (dists > thresh).astype(int)
        else:
            return dists
    

def shift(x, n):
    if type(x) is np.ndarray:
        if len(x.shape) == 1 or x.shape[0] == 1 or x.shape[1] == 1:
            return np.reshape(np.array(shift(x.tolist(), n)), x.shape)
        else:
            return np.array([shift(y, n) for y in x])
    elif type(x) is pd.DataFrame:
        return pd.DataFrame(index=x.index, columns=x.columns, data=shift(x.values, n))
    elif type(x) is pd.Series:
        return pd.Series(index=x.index, data=shift(x.values, n))
    
    if n == 0 or len(x) == 0:
        return x
    elif abs(n) > len(x):
        return np.zeros([len(x)], dtype=type(x[0])).tolist()
    elif n > 0:
        return np.zeros([n], dtype=type(x[0])).tolist() + x[:-n]
    else:
        return x[-n:] + np.zeros([-n], dtype=type(x[0])).tolist()


def listnum2group(subject, list, listgroups):
    if type(listgroups[0]) is list:
        return listgroups[subject][list]
    else:
        return listgroups[list]


def filter_egg(data, g, listgroups):
    p = data.get_pres_items().reset_index()
    r = data.get_rec_items().reset_index()

    p['Group'] = p.apply(lambda x: listnum2group(x['Subject'], x['List'], listgroups), axis=1)
    r['Group'] = r.apply(lambda x: listnum2group(x['Subject'], x['List'], listgroups), axis=1)

    p = p.query('Group == @g').set_index(['Subject', 'List']).drop('Group', axis=1)
    r = r.query('Group == @g').set_index(['Subject', 'List']).drop('Group', axis=1)

    return p, r


def recall_accuracy_near_boundaries(data, bounds, listgroups, maxlag=10):
    results = {}
    for g in np.unique(listgroups):
        p, r = filter_egg(data, g, listgroups)
        
        idx = pd.Index(np.unique(p.index.get_level_values('Subject')), name='Subject')
        results[g] = pd.DataFrame(index=idx, columns=np.arange(-maxlag, maxlag + 1), data=np.zeros([len(idx), 2 * maxlag + 1]))

        for i in range(p.shape[0]):
            for lag in range(-maxlag, maxlag + 1):
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    results[g].loc[p.index.get_level_values('Subject')[i], lag] = np.mean(np.array([x in r.iloc[i].values for x in p.iloc[i]])[shift(bounds.iloc[i].astype(bool).values, lag)])
        
    return results


def get_boundaries(n_stddev):
    boundary_fname = os.path.join(datadir, 'scratch', f'boundaries_{n_stddev}.pkl')
    if os.path.exists(boundary_fname):
        with open(boundary_fname, 'rb') as f:
            boundaries, accuracy_near_boundaries = pickle.load(f)
    else:
        data = fetch_data()
        boundaries = {c: get_event_boundaries(data[c], n_stddev=n_stddev) for c in data.keys()}

        accuracy_near_boundaries = {}
        if np.char.isnumeric(str(n_stddev)):
            for cond in tqdm(non_adaptive_exclude_random):
                accuracy_near_boundaries[cond] = {}
                for feature in tqdm(non_adaptive_exclude_random):
                    accuracy_near_boundaries[cond][feature] = recall_accuracy_near_boundaries(data[cond], boundaries[cond][feature], listgroups[cond], maxlag=15)

        with open(boundary_fname, 'wb') as f:
            pickle.dump([boundaries, accuracy_near_boundaries], f)
    
    return boundaries, accuracy_near_boundaries


def ttest(x, y, x_col=None, y_col=None, x_lists=None, y_lists=None, independent_sample=True, n_iter=1000, alpha=0.05):
    x = x.data
    y = y.data

    if x_lists is not None:
        if type(x_lists) is not list:
            x_lists = [x_lists]
        x = x.query('List in @x_lists')
    if y_lists is not None:
        if type(y_lists) is not list:
            y_lists = [y_lists]
        y = y.query('List in @y_lists')
    
    x = x.groupby('Subject').mean()
    y = y.groupby('Subject').mean()

    if x_col is not None:
        x = x[x_col]
    if y_col is not None:
        y = y[y_col]

    if independent_sample: 
        tfun = stats.ttest_ind
        df = len(x) + len(y) - 2        
    else:
        tfun = stats.ttest_rel 
        df = len(x) - 1
    result = tfun(x, y)

    # cohen's d
    x_mean = np.mean(x, axis=0)
    y_mean = np.mean(y, axis=0)
    x_var = np.var(x, ddof=1, axis=0)
    y_var = np.var(y, ddof=1, axis=0)
    pooled_sd = np.sqrt(((len(x) - 1) * x_var + (len(y) - 1) * y_var) / df)
    d = (x_mean - y_mean) / pooled_sd

    try:
        d = d[0]
    except:
        pass
    
    # bootstrap-estimated 95% confidence interval for t-statistic
    t_dist = np.zeros([n_iter])
    for i in range(n_iter):
        if independent_sample:
            x_sample = x.sample(n=len(x), replace=True)
            y_sample = y.sample(n=len(y), replace=True)
            t_dist[i] = stats.ttest_ind(x_sample, y_sample)[0]
        else:
            inds = np.random.choice(len(x), size=len(x), replace=True)
            x_sample = x.iloc[inds]
            y_sample = y.iloc[inds]
            t_dist[i] = stats.ttest_rel(x_sample, y_sample)[0]
    low_conf, high_conf = np.percentile(t_dist, [alpha * 50, 100 - (alpha * 50)])

    try:
        t = result.statistic[0]
        p = result.pvalue[0]
    except IndexError:
        t = result.statistic
        p = result.pvalue
    
    if p < 0.001:
        p_string = 'p < 0.001'
    else:
        p_string = f'p = {p:.3f}'

    print(f't({df}) = {t:.3f}, {p_string}, d = {d:.3f}, ' + '\mathrm{CI}' + f' = [{low_conf:.3f}, {high_conf:.3f}]')
    

def stack_fried_eggs(*args):
    if len(args) == 0:
        return None
    elif len(args) == 1:
        return args[0]
    
    data = [args[0].data]
    analysis = args[0].analysis
    list_length = args[0].list_length
    n_lists = args[0].n_lists
    n_subjects = args[0].n_subjects
    position = args[0].position

    idx_vals = args[0].data.index.values

    for i in range(1, len(args)):
        next_idx_vals = args[i].data.index.values
        next_idx_vals = [[x[0] + n_subjects, x[1]] for x in next_idx_vals]

        data.append(pd.DataFrame(args[i].data.values, index=pd.MultiIndex.from_tuples(next_idx_vals, names=args[i].data.index.names), columns=args[i].data.columns))
        n_subjects += args[i].n_subjects

        assert args[i].analysis == analysis, 'Analysis must be the same for all arguments'
        assert args[i].list_length == list_length, 'List length must be the same for all arguments'
        assert args[i].n_lists == n_lists, 'Number of lists must be the same for all arguments'
        assert args[i].position == position, 'Position must be the same for all arguments'

    return quail.FriedEgg(data=pd.concat(data, axis=0), analysis=analysis, list_length=list_length, n_lists=n_lists, n_subjects=n_subjects, position=position)


def merge_results(results, groups):
    merged_results = {}
    for g in groups.keys():
        next_fried_eggs = [results[c] for c in groups[g]]
        merged_results[g] = stack_fried_eggs(*next_fried_eggs)
    return merged_results


def create_clustering_df(results):
    conds = ['feature rich', 'category', 'size', 'length', 'first letter', 'color', 'location']
    decoder = {'category': 'category', 'size': 'size', 'length': 'wordLength', 'first letter': 'firstLetter', 'color': 'color', 'location': 'location'}

    dfs = []
    
    n_subjects = 0

    for c in conds:
        idx_vals = results['fingerprint'][c].data.index.values
        idx_vals = [[x[0] + n_subjects, x[1]] for x in idx_vals]

        x = pd.DataFrame(index=pd.MultiIndex.from_tuples(idx_vals, names=results['fingerprint'][c].data.index.names))
        x['Condition'] = c.capitalize()

        if c == 'feature rich':
            x['Feature clustering score'] = results['fingerprint'][c].data[list(decoder.values())].mean(axis=1).values
            x['Corrected feature clustering score'] = results['corrected fingerprint'][c].data[list(decoder.values())].mean(axis=1).values
        else:
            x['Feature clustering score'] = results['fingerprint'][c].data[decoder[c]].values
            x['Corrected feature clustering score'] = results['corrected fingerprint'][c].data[decoder[c]].values
        
        x['Temporal clustering score'] = results['fingerprint'][c].data['temporal'].values
        x['Recall probability'] = results['accuracy'][c].data.values

        dfs.append(x)

        n_subjects += results['accuracy'][c].n_subjects
    
    return pd.concat(dfs).reset_index()


# temporal correction procedure for fingerprints
def compute_temporally_corrected_fingerprints(results, results_by_list, listgroups, data=None):
    def same_item(pres, rec):
        return all([pres[k] == rec[k] for k in pres.keys() if k in rec]) and all([rec[k] == pres[k] for k in rec.keys() if k in pres])

    def get_position(x, presentations):
        if x is None:
            return []
        elif type(x) is pd.Series:
            return pd.Series([get_position(x.iloc[i], presentations) for i in range(len(x))], index=x.index)
        elif type(x) is pd.DataFrame:
            return pd.DataFrame([get_position(x.iloc[i], presentations.iloc[i]) for i in range(x.shape[0])], index=x.index, columns=x.columns)
        elif type(x) is dict:
            for i, p in enumerate(presentations):
                if same_item(p, x):
                    return i
        
        return -1
    
    def circshift(x):
        # circularly shift the values in x by a random amount
        if x is None:
            return []
        elif type(x) is pd.DataFrame:
            return pd.DataFrame([circshift(x.iloc[i]) for i in range(x.shape[0])], index=x.index, columns=x.columns)
        elif type(x) is pd.Series:
            if len(x) <= 1:
                return x
            else:
                shift = np.random.randint(0, len(x))
                return pd.Series(list(np.roll(x, shift)), index=x.index)

    def get_recalls(positions, presentations):
        if type(positions) is pd.Series:
            return pd.Series([np.nan if p == -1 else presentations[p] for p in positions], index=positions.index)
        elif type(positions) is pd.DataFrame:
            return pd.DataFrame([get_recalls(positions.iloc[i], presentations.iloc[i]) for i in range(positions.shape[0])], index=positions.index, columns=positions.columns)
    
    def temporally_corrected_fingerprints(x, n=N_ITER, savefile=None):
        if savefile is not None and os.path.exists(savefile):
            with open(savefile, 'rb') as f:
                observed, shuffled = pickle.load(f)
        else:
            pres = x.pres
            rec = x.rec
            
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                observed = x.analyze('fingerprint', permute=False)
            
            positions = get_position(rec, pres)

            shuffled = []
            for _ in tqdm(range(n)):
                next_x = deepcopy(x)
                next_x.rec = get_recalls(positions, circshift(pres))

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    shuffled.append(next_x.analyze('fingerprint', permute=False))
            
            if savefile is not None:
                with open(savefile, 'wb') as f:
                    pickle.dump([observed, shuffled], f)
        
        shuffled_stack = np.stack([s.data.values for s in shuffled], axis=2)
        observed_stack = np.stack([observed.data.values for _ in range(shuffled_stack.shape[2])], axis=2)
        
        x = pd.DataFrame(np.sum(observed_stack > shuffled_stack, axis=2) / shuffled_stack.shape[2],
                        index=observed.data.index, columns=observed.data.columns)
        
        corrected = deepcopy(observed)
        corrected.data = x
        return corrected
    
    def get_savefile(cond):
        scratch_dir = os.path.join(datadir, 'scratch')
        if not os.path.exists(scratch_dir):
            os.makedirs(scratch_dir)
        
        return os.path.join(scratch_dir, f'{cond}_shuffled_fingerprints.pkl')

    precomputed = True
    for k in results['fingerprint'].keys():
        if not os.path.exists(get_savefile(k)):
            precomputed = False
            break
    
    if not precomputed:
        data = fetch_data()
        for k in data.keys():
            temporally_corrected_fingerprints(data[k], savefile=get_savefile(k))
    
    for k in results['fingerprint'].keys():
        if 'corrected fingerprint' not in results:
            results['corrected fingerprint'] = {}
        if 'corrected fingerprint' not in results_by_list:
            results_by_list['corrected fingerprint'] = {}

        results_by_list['corrected fingerprint'][k] = temporally_corrected_fingerprints({}, savefile=get_savefile(k))
        results['corrected fingerprint'][k] = organize_by_listgroup(results_by_list['corrected fingerprint'][k], listgroups[k])
    
    return results, results_by_list

results, results_by_list = compute_temporally_corrected_fingerprints(results, results_by_list, listgroups)