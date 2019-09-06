from django.conf import settings
from django.db import models
from django.db.models import Min
from django.dispatch import receiver
from django.contrib.auth.models import User
import requests
from django.utils.text import slugify
from django.utils.translation import ugettext_lazy as _, ugettext
from django.core import validators
from channels import Group, Channel
from django.utils import timezone
from datetime import timedelta,datetime
from django_auth_lti.patch_reverse import reverse

from .groups import group_for_attempt
from .report_outcome import report_outcome_for_attempt, ReportOutcomeFailure, ReportOutcomeConnectionError

import os
import shutil
from zipfile import ZipFile
from lxml import etree
import re
import json
from collections import defaultdict

class NotDeletedManager(models.Manager):
    def get_queryset(self):
        return super(NotDeletedManager,self).get_queryset().filter(deleted=False)

class LTIConsumer(models.Model):
    url = models.URLField(blank=True,default='',verbose_name='Home URL of consumer')
    key = models.CharField(max_length=100,unique=True,verbose_name=_('Consumer key'),help_text=_('The key should be human-readable, and uniquely identify this consumer.'))
    secret = models.CharField(max_length=100,verbose_name=_('Shared secret'))
    deleted = models.BooleanField(default=False)

    objects = NotDeletedManager()

    def __str__(self):
        return self.key

    @property
    def resources(self):
        return Resource.objects.filter(context__consumer=self)

    def contexts_grouped_by_period(self):
        contexts = self.contexts.exclude(name='').annotate(creation=Min('resources__creation_time')).order_by('-creation')
        if not self.time_periods.exists():
            return [(None,contexts)]
        it = iter(self.time_periods.order_by('-end'))
        p = next(it)
        out = []
        lafter = []
        lduring = []
        for c in contexts.exclude(creation=None):
            while p is not None and c.creation<p.start:
                if len(lafter):
                    out.append((None,lafter))
                if len(lduring):
                    out.append((p,lduring))
                lafter = []
                lduring = []
                try:
                    p = next(it)
                except StopIteration:
                    p = None
            if p is None:
                lafter.append(c)
                continue
            if c.creation>p.end:
                lafter.append(c)
            else:
                lduring.append(c)
        if len(lafter):
            out.append((None,lafter))
        if len(lduring):
            out.append((p,lduring))
        no_creation = contexts.filter(creation=None)
        if no_creation.exists():
            out.append((None,no_creation[:]))
        groups = [(p,sorted(cs,key=lambda c:c.name.upper())) for p,cs in out]
        return groups

class ConsumerTimePeriod(models.Model):
    consumer = models.ForeignKey(LTIConsumer, related_name='time_periods', on_delete=models.CASCADE)
    start = models.DateTimeField()
    end = models.DateTimeField()
    name = models.CharField(max_length=300)

    class Meta:
        ordering = ['-end','-start']

class ExtractPackageMixin(object):
    extract_folder = 'extracted_zips'

    @property
    def extracted_path(self):
        return os.path.join(settings.MEDIA_ROOT,self.extract_folder,self.__class__.__name__,str(self.pk))

    @property
    def extracted_url(self):
        return '{}{}/{}/{}'.format(settings.MEDIA_URL,self.extract_folder,self.__class__.__name__,str(self.pk))

@receiver(models.signals.post_save)
def extract_package(sender,instance,**kwargs):
    if not issubclass(sender,ExtractPackageMixin):
        return
    if os.path.exists(instance.extracted_path):
        shutil.rmtree(instance.extracted_path)
    os.makedirs(instance.extracted_path)
    z = ZipFile(instance.package.file,'r')
    z.extractall(instance.extracted_path)


# Create your models here.
class Exam(ExtractPackageMixin,models.Model):
    title = models.CharField(max_length=300)
    package = models.FileField(upload_to='exams/',verbose_name='Package file')
    retrieve_url = models.URLField(blank=True,default='',verbose_name='URL used to retrieve the exam package')
    rest_url = models.URLField(blank=True,default='',verbose_name='URL of the exam on the editor\'s REST API')
    creation_time = models.DateTimeField(auto_now_add=True, verbose_name=_('Time this exam was created'))

    def __str__(self):
        return self.title

@receiver(models.signals.pre_save, sender=Exam)
def set_exam_name_from_package(sender,instance,**kwargs):
    z = ZipFile(instance.package.file,'r')
    with z.open('imsmanifest.xml','r') as manifest_file:
        manifest = etree.parse(manifest_file)
    instance.title = manifest.find('.//ims:title',namespaces={'ims':'http://www.imsglobal.org/xsd/imscp_v1p1'}).text


GRADING_METHODS = [
    ('highest',_('Highest score')),
    ('last',_('Last attempt')),
]

REPORT_TIMES = [
    ('immediately',_('Immediately')),
    ('oncompletion',_('On completion')),
    ('manually',_('Manually, by instructor')),
]
REPORTING_STATUSES = [
    ('reporting',_('Reporting scores')),
    ('error',_('Error encountered')),
    ('complete',_('All scores reported')),
]

SHOW_SCORES_MODES = [
    ('always',_('Always')),
    ('complete',_('When attempt is complete')),
    ('never',_('Never')),
]

class LTIContext(models.Model):
    consumer = models.ForeignKey(LTIConsumer,related_name='contexts', on_delete=models.CASCADE)
    context_id = models.CharField(max_length=300)
    name = models.CharField(max_length=300)
    label = models.CharField(max_length=300)
    instance_guid = models.CharField(max_length=300)

    def __str__(self):
        if self.name == self.label:
            return self.name
        else:
            return '{} ({})'.format(self.name, self.label)

class Resource(models.Model):
    resource_link_id = models.CharField(max_length=300)
    exam = models.ForeignKey(Exam,blank=True,null=True,on_delete=models.SET_NULL)
    context = models.ForeignKey(LTIContext,blank=True,null=True,on_delete=models.SET_NULL,related_name='resources')
    title = models.CharField(max_length=300,default='')
    description = models.TextField(default='')

    creation_time = models.DateTimeField(auto_now_add=True, verbose_name=_('Time this resource was created'))

    grading_method = models.CharField(max_length=20,choices=GRADING_METHODS,default='highest',verbose_name=_('Grading method'))
    include_incomplete_attempts = models.BooleanField(default=True,verbose_name=_('Include incomplete attempts in grading?'))
    show_marks_when = models.CharField(max_length=20, default='always', choices=SHOW_SCORES_MODES, verbose_name=_('When to show scores to students'))
    allow_review_from = models.DateTimeField(blank=True, null=True, verbose_name=_('Allow students to review attempts from'))
    report_mark_time = models.CharField(max_length=20,choices=REPORT_TIMES,default='immediately',verbose_name=_('When to report scores back'))

    max_attempts = models.PositiveIntegerField(default=0,verbose_name=_('Maximum attempts per user'))

    num_questions = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-creation_time','title']

    def __str__(self):
        if self.exam:
            return str(self.exam)
        elif self.context:
            return _('Resource in "{}" - no exam uploaded').format(self.context.name)
        else:
            return ugettext('Resource with no context')

    @property
    def slug(self):
        if self.exam:
            return slugify(self.exam.title)
        else:
            return 'resource'

    def grade_user(self,user):
        methods = {
            'highest': self.grade_highest,
            'last': self.grade_last,
        }
        attempts = self.attempts.filter(user=user)
        if not self.include_incomplete_attempts:
            attempts = attempts.filter(completion_status='completed')
        if not attempts.exists():
            return 0
        return methods[self.grading_method](user,attempts)

    def grade_highest(self,user,attempts):
        return attempts.aggregate(highest_score=models.Max('scaled_score'))['highest_score']

    def grade_last(self,user,attempts):
        return attempts.order_by('-start_time').first().scaled_score

    def students(self):
        return User.objects.filter(attempts__resource=self).distinct().order_by('last_name','first_name')

    def can_start_new_attempt(self,user):
        if self.max_attempts==0:
            return True
        return self.attempts.filter(user=user).count()<self.max_attempts or AccessToken.objects.filter(resource=self,user=user).exists()

    def user_data(self,user):
        return LTIUserData.objects.filter(resource=self,user=user).last()

    def part_hierarchy(self):
        """
            Returns an object
                {
                    question_num: {
                        part_num: {
                            gaps: [list of gap indices],
                            steps: [list of step indices]
                        }
                    }
                }
        """
        paths = sorted(set(e['value'] for e in ScormElement.objects.filter(attempt__resource=self,key__regex=r'cmi.interactions.[0-9]+.id').values('value')),key=lambda x:(len(x),x))
        re_path = re.compile(r'q([0-9]+)p([0-9]+)(?:g([0-9]+)|s([0-9]+))?')
        out = defaultdict(lambda: defaultdict(lambda: {'gaps':[],'steps':[]}))
        for path in paths:
            m = re_path.match(path)
            question_index = m.group(1)
            part_index = m.group(2)
            gap_index = m.group(3)
            step_index = m.group(4)
            p = out[question_index][part_index]
            if m.group(3):
                p['gaps'].append(gap_index)
            elif m.group(4):
                p['steps'].append(step_index)
    
        return out

    def last_activity(self):
        if self.attempts.exists():
            return self.attempts.order_by('-start_time').first().start_time
        else:
            return self.creation_time

    def time_since_last_activity(self):
        now = timezone.now()
        diff = now - self.last_activity()
        return diff

    def is_new(self):
        return self.time_since_last_activity().days < 7

    def is_old(self):
        return self.time_since_last_activity().days > 14

class ReportProcess(models.Model):
    resource = models.ForeignKey(Resource,on_delete=models.CASCADE,related_name='report_processes')
    status = models.CharField(max_length=10,choices=REPORTING_STATUSES,default='reporting',verbose_name=_("Current status of the process"))
    time = models.DateTimeField(auto_now_add=True,verbose_name=_("Time the reporting process started"))
    response = models.TextField(blank=True,verbose_name=_("Description of any error"))
    dismissed = models.BooleanField(default=False,verbose_name=_('Has the result of this process been dismissed by the instructor?'))

    class Meta:
        ordering = ['-time',]

COMPLETION_STATUSES = [
    ('not attempted',_('Not attempted')),
    ('incomplete',_('Incomplete')),
    ('completed',_('Complete')),
]

class AccessToken(models.Model):
    user = models.ForeignKey(User,on_delete=models.CASCADE,related_name='access_tokens')
    resource = models.ForeignKey(Resource,on_delete=models.CASCADE,related_name='access_tokens')

class LTIUserData(models.Model):
    consumer = models.ForeignKey(LTIConsumer,on_delete=models.CASCADE,null=True)
    user = models.ForeignKey(User,on_delete=models.CASCADE,related_name='lti_data')
    resource = models.ForeignKey(Resource,on_delete=models.CASCADE)
    lis_result_sourcedid = models.CharField(max_length=200,default='',blank=True,null=True)
    lis_outcome_service_url = models.TextField(default='',blank=True,null=True)
    last_reported_score = models.FloatField(default=0)
    consumer_user_id = models.TextField(default='',blank=True,null=True)

class Attempt(models.Model):
    resource = models.ForeignKey(Resource,on_delete=models.CASCADE,related_name='attempts')
    exam = models.ForeignKey(Exam,on_delete=models.CASCADE,related_name='attempts',null=True)  # need to keep track of both resource and exam in case the exam later gets overwritten
    user = models.ForeignKey(User,on_delete=models.CASCADE,related_name='attempts')
    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(blank=True,null=True)

    completion_status = models.CharField(max_length=20,choices=COMPLETION_STATUSES,default='not attempted')
    completion_status_element = models.ForeignKey("ScormElement", on_delete=models.SET_NULL, related_name="current_completion_status_of", null=True)
    scaled_score = models.FloatField(default=0)
    scaled_score_element = models.ForeignKey("ScormElement", on_delete=models.SET_NULL, related_name="current_scaled_score_of", null=True)

    deleted = models.BooleanField(default=False)
    broken = models.BooleanField(default=False)

    objects = NotDeletedManager()

    class Meta:
        ordering = ['-start_time',]

    def __str__(self):
        return 'Attempt by "{}" on "{}"'.format(self.user,self.resource)

    def get_element_default(self,key,default=None):
        try:
            return self.scormelements.current(key).value
        except ScormElement.DoesNotExist:
            if callable(default):
                default = default()
            return default

    def completed(self):
        return self.completion_status=='completed'

    @property
    def raw_score(self):
        if self.remarked_parts.exists() or self.resource.discounted_parts.exists():
            total = 0
            for i in range(self.resource.num_questions):
                total += self.question_raw_score(i)
            return total

        return float(self.get_element_default('cmi.score.raw',0))

    @property
    def max_score(self):
        if self.resource.discounted_parts.exists():
            total = 0
            for i in range(self.resource.num_questions):
                total += self.question_max_score(i)
            return total

        return float(self.get_element_default('cmi.score.max', lambda: sum(self.question_max_score(i) for i in range(self.resource.num_questions))))

    def part_discount(self,part):
        return self.resource.discounted_parts.filter(part=part).first()

    def part_paths(self):
        return set(e['value'] for e in self.scormelements.filter(key__regex='cmi.interactions.[0-9]+.id').values('value').distinct())

    def part_hierarchy(self):
        """
            Returns an object
                {
                    question_num: {
                        part_num: {
                            gaps: [list of gap indices],
                            steps: [list of step indices]
                        }
                    }
                }
        """
        paths = sorted(self.part_paths(),key=lambda x:(len(x),x))
        re_path = re.compile('q(\d+)p(\d+)(?:g(\d+)|s(\d+))?')
        out = defaultdict(lambda: defaultdict(lambda: {'gaps':[],'steps':[]}))
        for path in paths:
            m = re_path.match(path)
            p = out[m.group(1)][m.group(2)]
            if m.group(3):
                p['gaps'].append(m.group(3))
            elif m.group(4):
                p['steps'].append(m.group(4))

        return out

    def part_gaps(self,part):
        if not re.match(r'^q\d+p\d+$',part):
            return []
        gaps = [g for g in self.part_paths() if g.startswith(part+'g')]
        return gaps

    def part_interaction_id(self,part):
        id_element = self.scormelements.filter(key__regex='cmi.interactions.[0-9]+.id',value=part).first()
        n = re.match(r'cmi.interactions.(\d+).id',id_element.key).group(1)
        return n

    def part_raw_score(self,part):
        discounted = self.part_discount(part)
        if discounted:
            return self.part_max_score(part)

        remarked = self.remarked_parts.filter(part=part)
        if remarked.exists():
            return remarked.get().score

        if self.remarked_parts.filter(part__startswith=part+'g').exists() or self.resource.discounted_parts.filter(part__startswith=part+'g').exists():
            gaps = self.part_gaps(part)
            return sum(self.part_raw_score(g) for g in gaps)

        try:
            id = self.part_interaction_id(part)
        except ScormElement.DoesNotExist:
            return 0

        score = self.get_element_default('cmi.interactions.{}.result'.format(id),0)
        return float(score)

    def part_max_score(self,part):
        discounted = self.part_discount(part)
        if discounted:
            if discounted.behaviour == 'remove':
                return 0

        if DiscountPart.objects.filter(part__startswith=part+'g').exists():
            gaps = self.part_gaps(part)
            return sum(self.part_max_score(g) for g in gaps)

        try:
            id = self.part_interaction_id(part)
        except ScormElement.DoesNotExist:
            return 0

        return float(self.get_element_default('cmi.interactions.{}.weighting'.format(id),0))

    def question_raw_score(self,n):
        _,raw,_,_ = self.calculate_question_score_info(n)
        return raw

    def calculate_question_score_info(self,n):
        qid = 'q{}'.format(n)
        if self.remarked_parts.filter(part__startswith=qid).exists() or self.resource.discounted_parts.filter(part__startswith=qid).exists():
            question_parts = [p for p in self.part_paths() if p.startswith(qid)]
            total_raw = 0.0
            total_max = 0.0
            for part in question_parts:
                if re.match(r'^q{}p\d+$'.format(n),part):
                    total_raw += self.part_raw_score(part)
                    total_max += self.part_max_score(part)
            raw_score = total_raw
            scaled_score = total_raw/total_max if total_max>0 else 0.0
            max_score = total_max
        else:
            raw_score = float(self.get_element_default('cmi.objectives.{}.score.raw'.format(n),0))
            scaled_score = float(self.get_element_default('cmi.objectives.{}.score.scaled'.format(n),0))
            max_score = float(self.get_element_default('cmi.objectives.{}.score.max'.format(n),0))

        completion_status = self.get_element_default('cmi.objectives.{}.completion_status'.format(n),'not attempted')

        return (scaled_score, raw_score, max_score, completion_status)

    def update_question_score_info(self,n):
        scaled_score,raw_score,max_score,completion_status = self.calculate_question_score_info(n)
        AttemptQuestionScore.objects.update_or_create(attempt=self,number=n,defaults={'scaled_score':scaled_score,'raw_score':raw_score,'max_score':max_score,'completion_status':completion_status})

    def question_score_info(self,n):
        try:
            return self.cached_question_scores.get(number=n)
        except AttemptQuestionScore.DoesNotExist:
            scaled_score, raw_score, max_score, completion_status = self.calculate_question_score_info(n)
            aqs = AttemptQuestionScore.objects.create(attempt = self, number = n, raw_score = raw_score, scaled_score = scaled_score, max_score = max_score, completion_status = completion_status)
            return aqs
        except AttemptQuestionScore.MultipleObjectsReturned:
            aqs = self.cached_question_scores.filter(number=n)
            n = aqs.count()
            aq = aqs[n]
            aqs[:n].delete()
            return aq

    def question_numbers(self):
        questions = self.scormelements.filter(key__regex='cmi.objectives.[0-9]+.id').values('key').distinct()
        re_number = re.compile(r'cmi.objectives.([0-9]+).id')
        numbers = sorted(set([re_number.match(q['key']).group(1) for q in questions]))
        return numbers

    def question_scores(self):
        return sorted([self.question_score_info(n) for n in self.question_numbers()],key=lambda x:int(x.number))

    def question_max_score(self,n):
        _,_,max_score,_ = self.calculate_question_score_info(n)
        return max_score

    def channels_group(self):
        return 'attempt-{}'.format(self.pk)

    def resume_allowed(self):
        if self.completed():
            return self.review_allowed()
        else:
            return True

    def review_allowed(self):
        if not self.should_show_scores():
            return False
        return self.resource.allow_review_from is None or timezone.now() >= self.resource.allow_review_from

    def should_show_scores(self):
        return self.resource.show_marks_when=='always' or (self.resource.show_marks_when=='complete' and self.completed())

class AttemptQuestionScore(models.Model):
    attempt = models.ForeignKey(Attempt,related_name='cached_question_scores', on_delete=models.CASCADE)
    number = models.IntegerField()
    raw_score = models.FloatField()
    scaled_score = models.FloatField()
    max_score = models.FloatField()
    completion_status = models.CharField(default='not attempted',max_length=20)

    class Meta:
        unique_together = (('attempt','number'),)

    def __str__(self):
        return '{}/{} on question {} of {}'.format(self.raw_score,self.max_score,self.number,self.attempt)

class RemarkPart(models.Model):
    attempt = models.ForeignKey(Attempt,related_name='remarked_parts', on_delete=models.CASCADE)
    part = models.CharField(max_length=20)
    score = models.FloatField()
    
    def __str__(self):
        return '{} on part {} in {}'.format(self.score, self.part, self.attempt)

def remark_update_scaled_score(sender,instance,**kwargs):
    attempt = instance.attempt
    question = int(re.match(r'^q(\d+)',instance.part).group(1))
    attempt.update_question_score_info(question)
    if attempt.max_score>0:
        scaled_score = attempt.raw_score/attempt.max_score
    else:
        scaled_score = 0
    if scaled_score != attempt.scaled_score:
        attempt.scaled_score = scaled_score
        attempt.save()
models.signals.post_save.connect(remark_update_scaled_score,sender=RemarkPart)
models.signals.post_delete.connect(remark_update_scaled_score,sender=RemarkPart)

DISCOUNT_BEHAVIOURS = [
    ('remove','Remove from total'),
    ('fullmarks','Award everyone full credit'),
]

class DiscountPart(models.Model):
    resource = models.ForeignKey(Resource,related_name='discounted_parts', on_delete=models.CASCADE)
    part = models.CharField(max_length=20)
    behaviour = models.CharField(max_length=10,choices=DISCOUNT_BEHAVIOURS,default='remove')

def discount_update_scaled_score(sender,instance,**kwargs):
    for attempt in instance.resource.attempts.all():
        question = int(re.match(r'^q(\d+)',instance.part).group(1))
        attempt.update_question_score_info(question)

        scaled_score = attempt.raw_score/attempt.max_score
        if scaled_score != attempt.scaled_score:
            attempt.scaled_score = scaled_score
            attempt.save()
models.signals.post_save.connect(discount_update_scaled_score,sender=DiscountPart)
models.signals.post_delete.connect(discount_update_scaled_score,sender=DiscountPart)

class ScormElementQuerySet(models.QuerySet):
    def current(self,key):
        """ Return the last value of this field """
        elements = self.filter(key=key).order_by('-time','-counter')
        if not elements.exists():
            raise ScormElement.DoesNotExist()
        else:
            return elements.first()

class ScormElementManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        return ScormElementQuerySet(self.model, using=self.db)

    def current(self,key):
        return self.get_queryset().current(key)

class ScormElement(models.Model):
    objects = ScormElementManager()

    attempt = models.ForeignKey(Attempt,on_delete=models.CASCADE,related_name='scormelements')
    key = models.CharField(max_length=200)
    value = models.TextField()
    time = models.DateTimeField()
    counter = models.IntegerField(default=0,verbose_name='Element counter to disambiguate elements with the same timestamp')
    current = models.BooleanField(default=True) # is this the latest version?

    class Meta:
        ordering = ['-time','-counter']

    def __str__(self):
        return '{}: {}'.format(self.key,self.value[:50]+(self.value[50:] and '...'))

    def newer_than(self, other):
        return self.time>other.time or (self.time==other.time and self.counter>other.counter)

@receiver(models.signals.post_save,sender=ScormElement)
def send_scorm_element_to_dashboard(sender,instance,created,**kwargs):
    Group(instance.attempt.channels_group()).send({
        "text": json.dumps({
            'key': instance.key,
            'value': instance.value,
            'time': instance.time.strftime('%Y-%m-%d %H:%M:%S'),
        })
    })

@receiver(models.signals.post_save,sender=ScormElement)
def scorm_set_score(sender,instance,created,**kwargs):
    if instance.key!='cmi.score.scaled' or not created:
        return

    if not (instance.attempt.scaled_score_element is None or instance.newer_than(instance.attempt.scaled_score_element)):
        return

    instance.attempt.scaled_score = float(instance.value)
    instance.attempt.scaled_score_element = instance
    instance.attempt.save()
    if instance.attempt.resource.report_mark_time == 'immediately':
        try:
            report_outcome_for_attempt(instance.attempt)
        except (ReportOutcomeFailure, ReportOutcomeConnectionError):
            pass

@receiver(models.signals.post_save,sender=ScormElement)
def scorm_set_completion_status(sender,instance,created,**kwargs):
    if instance.key!='cmi.completion_status' or not created:
        return

    if not (instance.attempt.completion_status_element is None or instance.newer_than(instance.attempt.completion_status_element)):
        return

    instance.attempt.completion_status = instance.value
    instance.attempt.completion_status_element = instance
    if instance.value=='completed' and instance.attempt.end_time is None:
        instance.attempt.end_time = timezone.now()
        group_for_attempt(instance.attempt).send({'text':json.dumps({
            'completion_status':'completed',
        })})
    instance.attempt.save()

    if instance.attempt.resource.report_mark_time == 'oncompletion' and instance.value=='completed':
        try:
            report_outcome_for_attempt(instance.attempt)
        except (ReportOutcomeFailure, ReportOutcomeConnectionError):
            pass

@receiver(models.signals.post_save,sender=ScormElement)
def scorm_set_num_questions(sender,instance,created,**kwargs):
    """ Set the number of questions for this resource - can only work this out once the exam has been run! """
    if not re.match(r'^cmi.objectives.([0-9]+).id$',instance.key) or not created:
        return

    number = int(re.match(r'q(\d+)',instance.value).group(1))+1
    resource = instance.attempt.resource
    
    if number>resource.num_questions:
        resource.num_questions = number
        resource.save()

class EditorLink(models.Model):
    name = models.CharField(max_length=200,verbose_name='Editor name')
    url = models.URLField(verbose_name='Base URL of the editor',unique=True)
    cached_available_exams = models.TextField(blank=True,editable=False,verbose_name='Cached JSON list of available exams from this editor')
    last_cache_update = models.DateTimeField(blank=True,editable=False,verbose_name='Time of last cache update')

    def __str__(self):
        return self.name

    def update_cache(self,bounce=True):
        if bounce and self.time_since_last_update().seconds<30:
            return

        if self.projects.exists():
            project_pks = [str(p.remote_id) for p in self.projects.all()]
            r = requests.get('{}/api/available-exams'.format(self.url),{'projects':project_pks})

            self.cached_available_exams = r.text
        else:
            self.cached_available_exams = '[]'
        self.last_cache_update = timezone.now()

    def time_since_last_update(self):
        if self.last_cache_update is None:
            return timedelta.max
        return timezone.now() - self.last_cache_update

    @property
    def available_exams(self):
        if self.time_since_last_update().seconds> 30:
            Channel("editorlink.update_cache").send({'pk':self.pk})
        if self.cached_available_exams:
            return json.loads(self.cached_available_exams)
        else:
            return []

class EditorLinkProject(models.Model):
    editor = models.ForeignKey(EditorLink,on_delete=models.CASCADE,related_name='projects',verbose_name='Editor that this project belongs to')
    name = models.CharField(max_length=200,verbose_name='Name of the project')
    description = models.TextField(blank=True,verbose_name='Description of the project')
    remote_id = models.IntegerField(verbose_name='ID of the project on the editor')
    homepage = models.URLField(verbose_name='URL of the project\'s homepage on the editor')
    rest_url = models.URLField(verbose_name='URL of the project on the editor\'s REST API')

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

@receiver(models.signals.pre_save,sender=EditorLink)
def update_editor_cache_before_save(sender,instance,**kwargs):
    instance.update_cache()

class StressTest(models.Model):
    resource = models.OneToOneField(Resource,on_delete=models.CASCADE,primary_key=True)

    class Meta:
        ordering = ['-resource__creation_time']

    def __str__(self):
        return self.resource.creation_time.strftime('%B %d, %Y %H:%M')

    def get_absolute_url(self):
        return reverse('view_stresstest',args=(self.pk,))

class StressTestNote(models.Model):
    stresstest = models.ForeignKey(StressTest,on_delete=models.CASCADE,related_name='notes')
    text = models.TextField()
    time = models.DateTimeField(auto_now_add=True)
