import os
import logging
from common.dataformat import Site

class Configuration(object):
    pass

logging.basicConfig(level = logging.INFO)

read_only = False

daemon_mode = False

num_threads = 32

show_time_profile = True

paths = Configuration()
paths.base = os.environ['DYNAMO_BASE']
paths.data = os.environ['DYNAMO_DATADIR']

mysqlhistory = Configuration()
mysqlhistory.db_params = {
    'config_file': '/etc/my.cnf',
    'config_group': 'mysql-dynamo',
    'db': 'dynamohistory'
}

webservice = Configuration()
webservice.x509_key = os.environ['X509_USER_PROXY']
webservice.num_attempts = 5

mysql = Configuration()
mysql.max_query_len = 100000 # allows up to 1M characters; allowing 90% safety margin

mysqlstore = Configuration()
mysqlstore.db_params = {
    'config_file': '/etc/my.cnf',
    'config_group': 'mysql-dynamo',
    'db': 'dynamo'
}

phedex = Configuration()
phedex.url_base = 'https://cmsweb.cern.ch/phedex/datasvc/json/prod'
phedex.subscription_chunk_size = 4.e+13 # 40 TB

dbs = Configuration()
dbs.url_base = 'https://cmsweb.cern.ch/dbs/prod/global/DBSReader'
#dbs.url_base = 'https://cmsweb.cern.ch/dbs/prod/phys03/DBSReader'

ssb = Configuration()
ssb.url_base = 'http://dashb-ssb.cern.ch/dashboard/request.py'

sitedb = Configuration()
sitedb.url_base = 'https://cmsweb.cern.ch/sitedb/data/prod'

popdb = Configuration()
popdb.url_base = 'https://cmsweb.cern.ch/popdb'

globalqueue = Configuration()
globalqueue.collector = 'cmsgwms-collector-global.cern.ch:9620'

weblock = Configuration()
weblock.sources = [
    ('https://cmst2.web.cern.ch/cmst2/unified/globallocks.json', 'LIST_OF_DATASETS'),
    ('https://cmst2.web.cern.ch/cmst2/unified-testbed/globallocks.json', 'LIST_OF_DATASETS'),
    ('https://cmst1.web.cern.ch/CMST1/lockedData/lockTestSamples.json', 'SITE_TO_DATASETS'),
    ('https://cmsweb.cern.ch/t0wmadatasvc/prod/dataset_locked', 'CMSWEB_LIST_OF_DATASETS'),
    ('https://cmsweb.cern.ch/t0wmadatasvc/replayone/dataset_locked', 'CMSWEB_LIST_OF_DATASETS'),
    ('https://cmsweb.cern.ch/t0wmadatasvc/replaytwo/dataset_locked', 'CMSWEB_LIST_OF_DATASETS')
]
weblock.lock = 'https://cmst2.web.cern.ch/cmst2/unified/globallocks.json.lock'

tape_sites = ['T1_*_MSS', 'T0_CH_CERN_MSS']
disk_sites = ['T2_*', 'T1_*_Disk', 'T0_CH_CERN_Disk']

inventory = Configuration()
inventory.refresh_min = 43200 # 12 hours
inventory.included_sites = disk_sites
inventory.excluded_sites = ['T2_CH_CERNBOX', 'T2_MY_UPM_BIRUNI', 'T1_US_FNAL_New_Disk']
inventory.included_groups = [
    'AnalysisOps', 'DataOps', 'FacOps', 'IB RelVal', 'RelVal',
    'B2G',
    'SMP',
    'b-physics',
    'b-tagging',
    'caf-alca',
    'caf-comm',
    'caf-lumi',
    'caf-phys',
#    'deprecated-ewk',
#    'deprecated-qcd',
#    'deprecated-undefined',
    'dqm',
    'e-gamma_ecal',
    'exotica',
    'express',
    'forward',
    'heavy-ions',
    'higgs',
    'jets-met_hcal',
    'local',
    'muon',
    'susy',
    'tau-pflow',
    'top',
    'tracker-dpg',
    'tracker-pog',
    'trigger',
    'upgrade'
]

def group_partitioning(inventory, name):
    group = inventory.groups[name]
    return lambda r: r.group == group

# list of (partition name, partitioning function generator)
inventory.partitions = [
    ('AnalysisOps', group_partitioning),
    ('DataOps', group_partitioning),
    ('RelVal', group_partitioning),
    ('caf-comm', group_partitioning),
    ('caf-alca', group_partitioning),
    ('local', group_partitioning),
    ('IB RelVal', group_partitioning),
    ('Tape', lambda dummy1, dummy2: lambda r: r.site.storage_type == Site.TYPE_MSS),
    ('Unsubscribed', lambda dummy1, dummy2: lambda r: r.group is None)
]

demand = Configuration()
demand.access_history = Configuration()
demand.access_history.increment = 24 * 3600 # 24 hours
demand.access_history.max_back_query = 7 # maximum number of dates interval to obtain records for; 7 days
# give weight of bin[1] to now - bin[0]
demand.weight_time_bins = [
    (3600 * 24 * 7, 0.1),
    (3600 * 24 * 3, 0.5),
    (3600 * 24, 0.7),
    (3600 * 12, 1.)
]
