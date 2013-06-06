from django.conf import settings

from mapstory.models import *
from mapstory.util import lazy_context

import re

@lazy_context
def _sections():
    return list(Section.objects.order_by('order'))

@lazy_context
def _resources():
    return list(Resource.objects.order_by('order'))

_ua = re.compile('MSIE (\d+)\.')

_page_settings = {
    'design_mode' : settings.DESIGN_MODE,
    'enable_analytics' : settings.ENABLE_ANALYTICS,
    'ACCOUNT_OPEN_SIGNUP' : getattr(settings, 'ACCOUNT_OPEN_SIGNUP', False),
    'ENABLE_SOCIAL_LOGIN' : getattr(settings, 'ENABLE_SOCIAL_LOGIN', False),
    'ENABLE_SHARE_THIS' : getattr(settings, 'ENABLE_SHARE_THIS', True),
    'ENABLE_USER_VOICE' : getattr(settings, 'ENABLE_USER_VOICE', True),
    'cache_time': 60,
}

def page(req):
    '''provide base template context'''
    # detect old browsers - probably should fix to use more robust framework
    # for now, IE7 causes lots of trouble
    old_browser = False
    agent = req.META.get('HTTP_USER_AGENT','')
    if agent:
        match = _ua.search(agent)
        if match:
            try:
                version = int(match.group(1))
                old_browser = version < 8
            except:
                pass
        if old_browser:
            # the user has understood
            if 'iunderstand' in req.COOKIES:
                old_browser = False
    
    #@todo better of using a template_tag
    page = {
        'sections' : _sections(),
        'resources' : _resources(),
        'old_browser' : old_browser
    }

    ctx = dict(_page_settings)
    ctx['page'] = page
    
    return ctx
