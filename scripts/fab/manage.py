'''
To use:

  fab -f scripts/fab/manage.py -c .fab.dev get_layer:SlateGunDeaths

Where my .fab.dev config file had this inside:

  user = <ssh username>
  key_filename = /home/en/.ssh/id_rsa
  port = <the port to use for ssh>

'''
from fabric.operations import *
from fabric.context_managers import *
from fabric.api import *
from fabric.utils import abort
import os

env.use_ssh_config=True
env.hosts = ['mapstory.dev.opengeo.org']
env.deploy_user = 'geonode'
env.activate = 'source ~geonode/geonode/bin/activate'
if 'data_dir' not in env:
    env.data_dir = '../geonode/gs-data'

mapstory = os.path.dirname(__file__) + "/../.."
user_home = '/home/%s' % env.deploy_user

if not os.path.exists(env.data_dir):
    abort('env.data_dir "%s" does not exist' % env.data_dir)

#
# helpers
#
def lscript(name):
    with lcd(mapstory):
        local('DJANGO_SETTINGS_MODULE=mapstory.settings PYTHONPATH=.. python scripts/%s' % name)
        
def script(name):
    virtualenv('DJANGO_SETTINGS_MODULE=mapstory.settings PYTHONPATH=%s python mapstory/scripts/%s' % (user_home, name))

def virtualenv(command):
    with cd(user_home):
        sudo('%s && %s' % (env.activate, command), user = env.deploy_user)
        
#
# tasks
#
def update_geonode_client():
    virtualenv('JAVA_HOME=/usr/lib/jvm/java-6-sun ./do_update.sh')
    
def get_layer(layer):
    script('export_layer.py %s' % layer)
    pkg = '%s-extract.zip' % layer
    rpkg = '%s/%s' % (user_home,pkg)
    get(rpkg,'.')
    lscript('import_layer.py %s' % (pkg, ))
    sudo('rm %s' % rpkg,user = env.deploy_user)

def get_map(id):
    pkg = 'map-%s-extract.zip' % id
    script('export_maps.py %s' % pkg)
    rpkg = '%s/%s' % (user_home,pkg)
    get(rpkg,'.')
    lscript('import_maps.py %s' % (pkg, ))
    sudo('rm %s' % rpkg,user = env.deploy_user)