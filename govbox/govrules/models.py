from django.db import models
from django.contrib.auth.models import User, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
# from django.contrib.govinterface.models import LogEntry
from polymorphic.models import PolymorphicModel
from govrules.views import execute_proposal


class CommunityIntegration(PolymorphicModel):
    community_name = models.CharField('team_name', 
                              max_length=1000)


class CommunityUser(User, PolymorphicModel):
        
    readable_name = models.CharField('readable_name', 
                                      max_length=300)
    
    community_integration = models.ForeignKey(CommunityIntegration,
                                   models.CASCADE)
        
        
    def save(self, *args, **kwargs):
        permission = Permission.objects.get(name='Can add proposal')        
        super(User, self).save(*args, **kwargs)
        self.user_permissions.add(permission)
        
        


class Community(models.Model):
    name = models.CharField('name', max_length=150, unique=True)
    
    users = models.ManyToManyField(
        User,
        verbose_name='users',
        blank=True,
    )
    
    community_integration = models.ForeignKey(CommunityIntegration,
        models.CASCADE,
        verbose_name='community_integration'
    )
    
    
    class Meta:
        verbose_name = 'community'
        verbose_name_plural = 'community'

    def __str__(self):
        return self.name


class Proposal(models.Model):
    community = models.ForeignKey(Community, 
        models.CASCADE,
        verbose_name='community',
    )
    
    creators = models.ManyToManyField(
        User,
        verbose_name='creators',
        blank=True,
    )
    
    content_type = models.ForeignKey(
        ContentType,
        models.CASCADE,
        verbose_name='content type',
    )
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')
    
    ADD = 'add'
    CHANGE = 'change'
    VIEW = 'view'
    DELETE = 'delete'
    
    ACTIONS = [
            (ADD, 'add'),
            (CHANGE, 'change'),
            (VIEW, 'view'),
            (DELETE, 'delete')
        ]
    
    action = models.CharField(choices=ACTIONS, max_length=10)
    
    
    class Meta:
        verbose_name = 'proposal'
        verbose_name_plural = 'proposal'

    def __str__(self):
        return ' '.join([self.action, str(self.content_type), 'to', self.community.name])


    def save(self, *args, **kwargs):
        super(Proposal, self).save(*args, **kwargs)
        
        for rule in Rule.objects.filter(community=self.community):
            exec(rule.code)


class Rule(models.Model):
    
    community = models.ForeignKey(Community, 
        models.CASCADE,
        verbose_name='community',
    )
    
    code = models.TextField()
    
    
    
class Post(models.Model):
    
    community = models.ForeignKey(Community, 
        models.CASCADE,
        verbose_name='community',
    )
    
    author = models.ForeignKey(
        User,
        models.CASCADE,
        verbose_name='author',
    )
    
    text = models.TextField()
    
    
    class Meta:
        verbose_name = 'post'
        verbose_name_plural = 'post'

    def __str__(self):
        return ' '.join([self.author.username, 'wrote', self.community.name])


    