# support for client config migrations
#
#
#
import json
import optparse # for 2.6 support
import os
import sys

from django.core import serializers
from geonode.maps.models import MapLayer
from geonode.maps.models import Map


os.environ['DJANGO_SETTINGS_MODULE'] = "mapstory.settings"

class ConfigMigration(object):

    query_set = None

    dry_run = True

    def do_backup(self):
        'dump serialized models before changing them'
        if not self.query_set:
            raise Exception('need a query_set defined')
        backup_name = '%s_backup.json' % self.__class__.__name__
        if not self.opts.force_backup and os.path.exists(backup_name):
            raise Exception('backup file exists, please delete : %s' % backup_name)
        data = serializers.serialize("json", self.query_set)
        with open(backup_name, 'w') as fp:
            fp.write(data)
        print 'backed up %s models to %s' % (self.query_set.count(), backup_name)

    def restore(self):
        'restore from serialized models'
        backup_name = '%s_backup.json' % self.__class__.__name__
        if not os.path.exists(backup_name):
            raise Exception('no backup present')
        for obj in serializers.deserialize("json", open(backup_name).read()):
            print 'restored', obj
            obj.save()

    def _configure(self, opts, args):
        'do general config'
        self.dry_run = not opts.run
        self.opts = opts
        self.args = args

    def configure(self, opts, args):
        'override for specific config'
        pass

    def init(self):
        'if query_set must be more dynamic, etc'
        pass

    def _run(self):
        'general run'
        self.init()
        if self.dry_run:
            msg = 'dry run, will not make any changes'
            print '*' * len(msg)
            print msg
            print '*' * len(msg)
        if not self.dry_run:
            self.do_backup()
        self.run()

    def process_model(self, m):
        raise Exception('implement process_model')

    def run(self):
        'override me or process_model'
        print 'processing %s models' % self.query_set.count()
        for m in self.query_set:
            self.process_model(m)
            if not self.dry_run:
                m.save()

    def configure_options(self, parser):
        pass

    def main(self, options, args):
        self._configure(options, args)
        self.configure(options, args)
        if options.restore:
            self.restore()
        else:
            self._run()


class BaseLayerNameMigration(ConfigMigration):
    'migrate base layer names'

    name_mappings = {
        'Wayne': 'Naked Earth',
        'bluemarble': 'Satellite Imagery'
    }

    def init(self):
        self.query_set = MapLayer.objects.filter(name__in=self.name_mappings.keys())

    def process_model(self, m):
        new_name = self.name_mappings[m.name]
        print 'updating %s to %s, in map: %s' % (m.name, new_name, m.map.id)
        layer_params = json.loads(m.layer_params)
        layer_params['title'] = new_name
        layer_params['args'][0] = new_name
        m.layer_params = json.dumps(layer_params)
        m.name = new_name


class RemoveMapProperties(ConfigMigration):
    'remove any gxp_mapproperties tools from stored map configuration'

    query_set = Map.objects.filter(tools_params__contains='gxp_mapproperties')

    def process_model(self, m):
        if not m.tools_params:
            return
        tools_params = json.loads(m.tools_params)
        filtered = [ t for t in tools_params if not t['ptype'] == 'gxp_mapproperties']
        if filtered != tools_params:
            print 'will remove tools from', m.title, m.id
        m.tools_params = json.dumps(filtered)


class UseVirtualOWSURL(ConfigMigration):
    'update ows_url to point to virtual service'

    query_set = MapLayer.objects.filter(ows_url='/geoserver/wms')

    def process_model(self, m):
        m.ows_url = '/geoserver/geonode/%s/wms' % m.name
        print 'updating %s to use ows_url=%s (map=%s)' % (m.name, m.ows_url, m.map.id)


def _migrations():
    migrations = []
    for v in globals().items():
        try:
            if v[1] != ConfigMigration and issubclass(v[1], ConfigMigration):
                migrations.append(v)
        except TypeError:
            pass
    return migrations


def _print_help(parser, exit=0):
    parser.print_help()
    print
    print 'available migrations:'
    print '\t','\n\t'.join( ['%s - %s' % (v[0],v[1].__doc__) for v in _migrations()])
    sys.exit(exit)


if __name__ == '__main__':
    parser = optparse.OptionParser("usage: %prog [help] [options] migration")
    parser.add_option('-r', '--run',
        help='Unless specified, will only print what would happen',
        action='store_true', default=False)
    parser.add_option('-l', '--restore',
        help='Restore model dump',
        action='store_true', default=False)
    parser.add_option('-f', '--force-backup',
        help='Overwrite any existing backup',
        action='store_true', default=False)

    args = sys.argv[1:]
    if not len(args):
        _print_help(parser)
    help = args[0] == 'help'
    if help:
        args.pop(0)
        if not len(args):
            print 'help requires a migration name'
            sys.exit(1)
    migrations = dict([ (v[0].lower(),v[1]) for v in _migrations() ])
    migration = args.pop(0)
    if not migration.lower() in migrations:
        print 'migration not found:', migration
        _print_help(parser, 1)
    migration = migrations[migration.lower()]()
    migration.configure_options(parser)
    if help: _print_help(parser)
    migration.main(*parser.parse_args(args))