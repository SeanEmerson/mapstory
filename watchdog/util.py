from announcements.models import Announcement
from django.contrib.auth.models import User
from datetime import datetime

def geoserver_trouble_announcement(enable):
    user = User.objects.filter(is_superuser=True)[0]
    ann, created = Announcement.objects.get_or_create(title='Mapping Service Experiencing Heavy Traffic', creator=user)
    if created:
        ann.content = 'It is likely temporarily down, but should be up soon. If this problem persists please let the administrators know.'
        ann.dismissal_type = Announcement.DISMISSAL_NO
        ann.site_wide = True
    if enable:
        # make sure this gets noticed
        ann.publish_start = datetime(1970, 1, 1)
        ann.save()
    else:
        ann.delete()