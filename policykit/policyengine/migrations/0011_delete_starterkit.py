# Generated by Django 3.2.2 on 2021-08-16 18:00

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('discourse', '0002_delete_discoursestarterkit'),
        ('reddit', '0002_delete_redditstarterkit'),
        ('discord', '0004_delete_discordstarterkit'),
        ('slack', '0004_delete_slackstarterkit'),
        ('policyengine', '0010_auto_20210816_1800'),
    ]

    operations = [
        migrations.DeleteModel(
            name='StarterKit',
        ),
    ]
