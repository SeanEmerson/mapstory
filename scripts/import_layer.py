'''Import an extracted layer and related resources'''
from django.conf import settings
from django.contrib.auth.models import User
from django.core import serializers
from django.db import transaction

from geonode.maps.models import Layer

from mapstory.models import PublishingStatus

from optparse import OptionParser
import json
import psycopg2
import os
import tempfile
import shutil
import glob
import requests
import xml.etree.ElementTree as ET

from update_thumb_specs import make_thumbnail_updater

class ImportException(Exception):
    pass

def _get_user(**kw):
    try:
        return User.objects.get(**kw)
    except User.DoesNotExist:
        return None

@transaction.commit_on_success
def import_layer(gs_data_dir, conn, layer_tempdir, layer_name, owner_name, gs_user, gs_pass,
                 no_password=False, chown_to=None, do_django_layer_save=True,
                 th_from_string=None, th_to_string=None):

    owner = None
    if owner_name:
        owner = _get_user(username=owner_name)
        if not owner:
            raise ImportException('specified owner_name "%s" does not exist' % owner_name)

    print 'importing layer: %s' % layer_name
    gspath = lambda *p: os.path.join(gs_data_dir, *p)

    temppath = lambda *p: os.path.join(layer_tempdir, *p)

    restore_string = 'pg_restore --host=%s --dbname=%s --clean --username=%s %s < %s' % (
        settings.DB_DATASTORE_HOST, settings.DB_DATASTORE_DATABASE, settings.DB_DATASTORE_USER,
        no_password and '--no-password' or '',
        temppath('layer.dump'),
    )
    # can't check return value since pg_restore will complain if any drop statements fail :(
    os.system(restore_string)

    # rebuild the geometry columns entry
    with open(temppath("geom.info")) as fp:
        s = fp.read()
        geom_cols = eval(s)[0]
        
    cursor = conn.cursor()
    # f_table_catalog, f_table_schema, f_table_name
    cursor.execute("delete from geometry_columns where f_table_schema='%s' and f_table_name='%s'" % (geom_cols[1],geom_cols[2]))
    cursor.execute('insert into geometry_columns VALUES(%s)' % ','.join(["'%s'" % v for v in geom_cols]))

    # To the stylemobile!
    gs_style_url = "{0}/rest/workspaces/geonode/styles/".format(settings.GEOSERVER_BASE_URL)
    style_headers = {'content-type': 'application/vnd.ogc.sld+xml'}
    for f in glob.glob("%s/*.sld" % temppath('styles')):
        sld = open(f, 'r').read()
        r = requests.post(gs_style_url, data=sld, auth=(gs_user, gs_pass), headers=style_headers)

    # Now let's load the layers
    layer_headers = {'content-type': 'text/xml'}
    for ws in os.listdir(temppath('workspaces')):
        for store_name in os.listdir(temppath('workspaces',ws)):
            for layer_name in os.listdir(temppath('workspaces',ws,store_name)):
                gs_layer_url = "{0}/geoserver/rest/styles/".format(settings.GEOSERVER_BASE_URL)#workspaces/{1}/{2}/{3}".format(settings.GEOSERVER_BASE_URL, ws, store_name, layer_name)
                # Try loading the easy way first
                gs_layer_data = "<featureType><name>{0}</name></featureType>".format(layer_name)
                r = requests.post(gs_layer_url, data=gs_layer_data, auth=(gs_user, gs_pass), headers=layer_headers)                
                if not r.ok:
                    # Oh No. Something went wrong. Let's try defining attributes before giving up
                    layer_xml = ET.parse(temppath('workspaces',ws,store_name,layer_name,'featuretype.xml')).getroot()
                    # Remove unneeded parts from xml
                    for namespace_element in layer_xml.findall('namespace'):
                        layer_xml.remove(namespace_element)
                    for id_element in layer_xml.findall('id'):
                        layer_xml.remove(id_element)
                    # Now change those pesky little custom dates to the underlying big int typename
                    for attrib_binding in layer_xml.findall('binding'):
                        if attrib_binding.text == "org.geotools.data.postgis.PostGISDialect$XDate":
                            attrib_binding.text = "java.math.BigInteger"
                    r = requests.post(gs_layer_url, data=ET.tostring(layer_xml), auth=(gs_user, gs_pass), headers=layer_headers)
    # reload catalog
    Layer.objects.gs_catalog.http.request(settings.GEOSERVER_BASE_URL + "rest/reload",'POST')

    if do_django_layer_save:
        # now we can create the django model - must be done last when gscatalog is ready
        with open(temppath("model.json")) as fp:
            model_json = fp.read()
            layer = serializers.deserialize("json", model_json).next()

            if not owner:
                owner = _get_user(pk=layer.object.owner_id)
            if not owner:
                owner = _get_user(username='admin')
            if not owner:
                owner = User.objects.filter(is_superuser=True)[0]
            layer.object.owner = owner
            print 'Assigning layer to %s' % layer.object.owner
            layer_exists = Layer.objects.filter(typename=layer.object.typename)

            if not layer_exists:
                layer.save()
                print 'Layer %s saved' % layer_name
            else:
                print 'Layer %s already exists ... skipping model save' % layer_name

        # add thumbnail if exists in src and not destination
        try:
            layer = Layer.objects.get(typename='geonode:%s' % layer_name)
        except Layer.DoesNotExist:
            print 'Layer %s does not exist. Could not update thumbnail spec' % layer_name
        else:
            thumb_spec_path = temppath('thumb_spec.json')
            thumbnail = layer.get_thumbnail()
            if thumbnail:
                print 'thumbnail already exists for: %s ... skipping creation' % layer_name
            else:
                if os.path.isfile(thumb_spec_path):
                    with open(thumb_spec_path) as f:
                        thumb_spec = json.load(f)
                        layer.set_thumbnail(thumb_spec)
            
            # rename thumb_spec if asked to
            if (th_from_string is not None and th_to_string is not None):
                thumbnail = layer.get_thumbnail()
                if thumbnail:
                    updater =  make_thumbnail_updater(th_from_string, th_to_string)
                    updater(thumbnail)
                else:
                    print 'No thumbnail to update spec for layer: %s' % layer_name

    cursor.close()
    
    # Load layer status
    with open(temppath('publishingstatus.json'), 'r') as f:
            statuses = serializers.deserialize('json', f)
            for status in statuses:
                try:
                    # Is there already a publishing status?
                    ps = PublishingStatus.objects.get(layer=status.object.layer)
                    ps.status = status.object.status
                    ps.save()
                except PublishingStatus.DoesNotExist:
                    status.save()

if __name__ == '__main__':
    gs_data_dir = '/var/lib/geoserver/geonode-data/'

    parser = OptionParser('usage: %s [options] layer_import_file.zip' % __file__)
    parser.add_option('-d', '--data-dir',
                      dest='data_dir',
                      default=gs_data_dir,
                      help='geoserver data dir')
    parser.add_option('-P', '--no-password',
                      dest='no_password', action='store_true',
                      help='Add the --no-password option to the pg_restore'
                      'command. This assumes the user has a ~/.pgpass file'
                      'with the credentials. See the pg_restore man page'
                      'for details.',
                      default=False,
                      )
    parser.add_option('-c', '--chown-to',
                      dest='chown_to',
                      help='If set, chown the files copied into the'
                      'geoserver data directory to a particular'
                      'user. Assumes the user running is root or has'
                      'permission to do so. This is useful to chown the'
                      'files to something like tomcat6 afterwards.',
                      )
    parser.add_option('-L', '--skip-django-layer-save',
                      dest='do_django_layer_save',
                      default=True,
                      action='store_false',
                      help='Whether to skip loading the django layer model'
                      )
    parser.add_option('-f', '--thumbnail-from-string',
                      dest='th_from_string',
                      help='Used as the source string to use when replacing the thumb_spec',
                      )
    parser.add_option('-t', '--thumbnail-to-string',
                      dest='th_to_string',
                      help='Used as the replacement string to use when replacing the thumb_spec',
                      )
    parser.add_option('-o', '--owner-name',
                      dest='owner_name',
                      help='Set the layer owner to a user by this name - must exist',
                      )
    parser.add_option('-u', '--gs-user',
                      dest='gs_user',
                      help='GeoServer User to import data with.',
                      )
    parser.add_option('-p', '--gs-pass',
                      dest='gs_pass',
                      help='GeoServer Password to import data with.',
                      )

    (options, args) = parser.parse_args()
    if len(args) != 1:
        parser.error('please provide a layer extract zip file')

    if not os.path.exists(options.data_dir):
        parser.error(("geoserver data directory %s not found,"
                      "please specify one via the -d option") % options.data_dir)
    
    conn = psycopg2.connect("dbname='" + settings.DB_DATASTORE_DATABASE + 
                            "' user='" + settings.DB_DATASTORE_USER + 
                            "' password='" + settings.DB_DATASTORE_PASSWORD + 
                            "' port=" + settings.DB_DATASTORE_PORT + 
                            " host='" + settings.DB_DATASTORE_HOST + "'")

    zipfile = args.pop()
    tempdir = tempfile.mkdtemp()
    layer_name = zipfile[:zipfile.rindex('-extract')]
    os.system('unzip %s -d %s' % (zipfile, tempdir))
    success = False
    try:
        import_layer(options.data_dir, conn, tempdir, layer_name,
                     options.owner_name, options.gs_user, options.gs_pass,
                     options.no_password, options.chown_to,
                     options.do_django_layer_save,
                     options.th_from_string, options.th_to_string)
        success = True
    except ImportException, e:
        print e
    finally:
        if success:
            conn.commit()
        else:
            conn.rollback()
        conn.close()
        shutil.rmtree(tempdir)

