# logfile parsing routines
from django.core.mail import EmailMessage

from mapstory.watchdog.core import _config
from mapstory.watchdog.core import set_config
from mapstory.watchdog.models import get_logfile_model
from datetime import date
import hashlib
import itertools

LEVELS = set( [ ' %s ' for s in ['WARN', 'ERROR']])

_scan_exclude = (
    'org.geoserver.platform.ServiceException: Could not determine geoserver request from http request org.geoserver.monitor.MonitorServletRequest',
)


def is_logger_line(line, levels):
    return any([level in line
                for level in levels])


def _not_excluded(section):
    is_excluded = lambda line: any([ line.find(e) >= 0 for e in _scan_exclude])
    return not any([is_excluded(line) for line in section])


def log_scan():
    logpath = _config['GEOSERVER_LOG']
    today = date.today().strftime('%Y-%m-%d')
    with open(logpath, 'r') as fp:
        lines = iter(fp)
        for l in lines:
            if l.startswith(today):
                break
        sections = log_sections(itertools.chain([l], lines), gather_stack=1, levels=[' ERROR '])
    sections = filter(_not_excluded, sections)
    sections = [ '\t'.join(sub) for sub in sections ]
    body = '\n'.join(sections)
    EmailMessage(
        'Daily Error Summary',
        body,
        _config['FROM'],
        _config['TO'],
    ).send()


def log_sections(fileobj, gather_stack=None, levels=None):
    if levels is None:
        levels = LEVELS
    sections = []
    next_section = []
    gather_stack = gather_stack if gather_stack is not None else 10
    for line in fileobj:
        if is_logger_line(line, levels):
            if next_section:
                sections.append(next_section)
            next_section = [line]
        elif gather_stack >= len(next_section):
            next_section.append(line)
    if next_section:
        sections.append(next_section)
    return sections


def checksum(data, start, end):
    if isinstance(data, basestring):
        combined = '%s%s%s' % (start, end, data)
        return hashlib.md5(combined).hexdigest()
    elif isinstance(data, list):
        return checksum(
            ''.join([''.join(section) for section in data]),
            start, end)
    else:
        assert False, "Taking checksum of unknown type: %s" % type(data)


def is_valid(fileobj, logfile_model):
    """verify that the offset is still valid for the file. This may
    not be valid if the log file was truncted or rotated in between
    runs."""
    cur = fileobj.tell()
    start = logfile_model.offset_start
    end = logfile_model.offset_end
    try:
        # check if the logfile_model has a previous offset
        if logfile_model.offset_end == 0:
            return False
        # check that the end offset on the file is greater
        fileobj.seek(0, 2)
        fileend = fileobj.tell()
        if end > fileend:
            return False
        # verify the checksum
        n = end - start
        fileobj.seek(start)
        data = fileobj.read(n)
        if checksum(data, start, end) != logfile_model.checksum:
            return False
        return True
    finally:
        fileobj.seek(cur)


def read_next_sections(fileobj, logfile_model, gather_stack=None):
    offset = (is_valid(fileobj, logfile_model)
              and logfile_model.offset_end
              or 0)
    fileobj.seek(offset)
    sections = log_sections(fileobj, gather_stack=gather_stack)
    offset_end = fileobj.tell()
    return dict(
        offset_start=offset,
        offset_end=offset_end,
        sections=sections,
        )


def set_log_state_to_end_of_file():
    """Useful to reset the state of a logfile to the end, to pick up only new errors"""
    set_config()
    logpath = _config['GEOSERVER_LOG']
    logfile_model = get_logfile_model(logpath)
    with open(logpath) as fileobj:
        fileobj.seek(0, 2)
        end = fileobj.tell()
    new_checksum = checksum('', end, end)
    logfile_model.checksum = new_checksum
    logfile_model.offset_start = end
    logfile_model.offset_end = end
    logfile_model.save()
