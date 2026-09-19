from django.contrib import admin

from apps.retention.models import RetentionTask

admin.site.register(RetentionTask)
