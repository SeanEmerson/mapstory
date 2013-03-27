'''Import an extracted layer and related resources'''
import os.path
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
import subprocess
import tempfile
import shutil
import glob
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
def import_layer(conn, layer_tempdir, layer_name, owner_name,
                 no_password=False, do_django_layer_save=True,
                 th_from_string=None, th_to_string=None):

    owner = None
    if owner_name:
        owner = _get_user(username=owner_name)
        if not owner:
            raise ImportException('specified owner_name "%s" does not exist' % owner_name)

    print 'importing layer: %s' % layer_name

    cat = Layer.objects.gs_catalog

    temppath = lambda *p: os.path.join(layer_tempdir, *p)

    # @todo cannot rollback this via the connection transaction, must be done manually
    restore_string = 'pg_restore --host=%s --dbname=%s --clean --username=%s %s < %s' % (
        settings.DB_DATASTORE_HOST, settings.DB_DATASTORE_DATABASE, settings.DB_DATASTORE_USER,
        no_password and '--no-password' or '',
        temppath('layer.dump'),
    )
    # can't check return value since pg_restore will complain if any drop statements fail :(
    subprocess.call(restore_string, shell=True, env={'PGPASSWORD':settings.DB_DATASTORE_PASSWORD})

    # rebuild the geometry columns entry
    with open(temppath("geom.info")) as fp:
        s = fp.read()
        geom_cols = eval(s)[0]
        
    cursor = conn.cursor()
    # f_table_catalog, f_table_schema, f_table_name
    cursor.execute("delete from geometry_columns where f_table_schema='%s' and f_table_name='%s'" % (geom_cols[1],geom_cols[2]))
    cursor.execute('insert into geometry_columns VALUES(%s)' % ','.join(["'%s'" % v for v in geom_cols]))

    # To the stylemobile!
    # build a dict of id->style so we can match them up to layers later
    style_by_id = {}
    for f in glob.glob("%s/*.xml" % temppath('styles')):
        style_xml = ET.parse(f).getroot()
        filename = style_xml.find('filename').text
        style_name = style_xml.find('name').text
        sld = open(temppath('styles', filename), 'r').read()
        try:
            # @todo allow overwrite via the named argument?
            cat.create_style(style_name, sld)
        except Exception, ex:
            print "error creating style : %s" % ex
        id = style_xml.find('id').text
        style_by_id[id] = style_name


    # Now let's load the layers
    for ws in os.listdir(temppath('workspaces')):
        for store_name in os.listdir(temppath('workspaces',ws)):
            for layer_name in os.listdir(temppath('workspaces',ws,store_name)):

                # @todo allow overriding existing?
                if cat.get_layer(layer_name) is None:
                    # create the layer by reading the xml, stripping some specific
                    # ids, and posting to REST endpoint
                    # @todo didn't see nice way to do this via gsconfig
                    gs_feature_url = "%(base)srest/workspaces/%(workspace)s/datastores/%(datastore)s/featuretypes" % dict(
                        base = settings.GEOSERVER_BASE_URL,
                        workspace = ws,
                        datastore = store_name
                    )
                    feature_xml = ET.parse(temppath('workspaces',ws,store_name,layer_name,'featuretype.xml')).getroot()
                    # strip potentially foreign ids
                    for el in ('id', 'namespace', 'store'):
                        feature_xml.remove(feature_xml.find(el))
                    headers, response = cat.http.request(gs_feature_url, "POST", ET.tostring(feature_xml), { "Content-Type": "application/xml" })
                    if str(headers.status) != '201':
                        print gs_feature_url
                        print response
                        raise ImportException("Invald response from REST, layer creation: %s" % headers.status)
                else:
                    print "skipping feature type creation, layer already exists"

                # now set the styles - we have to track down what styles are in
                # use via the style ids :(
                layer_xml = ET.parse(temppath('workspaces',ws,store_name,layer_name,'layer.xml')).getroot()
                cat_layer = cat.get_layer(layer_name)
                # if these styles don't speak the 'name' attribute, they don't quack
                # quack!
                style = lambda el: type('style',(object,),{'name' : style_by_id[el.text]})
                cat_layer.styles = [ style(id) for id in layer_xml.findall('.//style/id') ]
                # this one can be by name
                cat_layer.default_style = style_by_id[layer_xml.find('.//defaultStyle/id').text]
                cat.save(cat_layer)

    # reload catalog
    Layer.objects.gs_catalog.http.request(settings.INTERNAL_GEOSERVER_BASE_URL + "rest/reload",'POST')

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
    parser.add_option('-P', '--no-password',
                      dest='no_password', action='store_true',
                      help='Add the --no-password option to the pg_restore'
                      'command. This assumes the user has a ~/.pgpass file'
                      'with the credentials. See the pg_restore man page'
                      'for details.',
                      default=False,
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
        import_layer(conn, tempdir, layer_name,
                     options.owner_name,
                     options.no_password,
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

