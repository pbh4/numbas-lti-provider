#<<<<<<< HEAD
#pbh4
#from __future__ import division
#import re
#

#=======
from django.conf import settings
#>>>>>>> upstream/master
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.shortcuts import redirect
from django_auth_lti.patch_reverse import reverse
from django.urls import reverse_lazy
from django.utils.decorators import available_attrs
from django.utils.translation import ugettext_lazy as _
from django.views import generic
from django_auth_lti.mixins import LTIRoleRestrictionMixin
from django_auth_lti.verification import is_allowed
from functools import wraps
from numbas_lti.models import Resource, Exam

#pbh
INSTRUCTOR_ROLES = getattr(settings,'LTI_INSTRUCTOR_ROLES',['instructor','urn:lti:role:ims/lis/Instructor','Instructor','Administrator','ContentDeveloper','Manager','TeachingAssistant'])

def get_lti_entry_url(request):
    return request.build_absolute_uri(reverse('lti_entry',exclude_resource_link_id=True))

def get_config_url(request):
    return request.build_absolute_uri(reverse('config_xml',exclude_resource_link_id=True))

def request_is_instructor(request):
    if request.user.is_superuser:
        return True
#<<<<<<< HEAD
#    return 'Instructor' in request.LTI.get('roles')
# pbh: Bb uses lowercase!
#    for role in request.LTI.get('roles'):
#        if re.match("(.*)(I|i)nstructor(.*)", role):
#            return True;
#=======
    return is_allowed(request,INSTRUCTOR_ROLES,False)
#>>>>>>> upstream/master

def static_view(template_name):
    return generic.TemplateView.as_view(template_name=template_name)

class LTIRoleOrSuperuserMixin(LTIRoleRestrictionMixin):
    def check_allowed(self, request):
        if request.user.is_superuser:
            return True
        else:
            return super(LTIRoleOrSuperuserMixin, self).check_allowed(request)

class MustBeInstructorMixin(LTIRoleOrSuperuserMixin):
#<<<<<<< HEAD
#    allowed_roles = ['Instructor','urn:lti:role:ims/lis/Instructor']
#=======
    allowed_roles = INSTRUCTOR_ROLES

def lti_role_or_superuser_required(allowed_roles, redirect_url=reverse_lazy('not_authorized'), raise_exception=False):
    def decorator(view_func):
        @wraps(view_func, assigned=available_attrs(view_func))
        def _wrapped_view(request, *args, **kwargs):
            if request.user.is_superuser or is_allowed(request, allowed_roles, raise_exception):
                return view_func(request, *args, **kwargs)
            
            return redirect(redirect_url)
        return _wrapped_view
    return decorator
#>>>>>>> upstream/master

class ManagementViewMixin(object):
    def get_context_data(self,*args,**kwargs):
        context = super(ManagementViewMixin,self).get_context_data(*args,**kwargs)
        context.update({
            'management_tab': self.management_tab
        })
        return context

class ResourceManagementViewMixin(ManagementViewMixin):
    context_object_name = 'resource'
    resource_pk_url_kwarg = 'pk'

    def get_resource(self):
        if self.model == Resource:
            return self.get_object()
        else:
            pk = self.kwargs.get(self.resource_pk_url_kwarg)
            return Resource.objects.get(pk=pk)

    def dispatch(self,*args,**kwargs):
        self.resource = self.get_resource()
        if not hasattr(self.request,'resource') or self.request.resource is None:
            self.request.resource = self.resource

        return super(ResourceManagementViewMixin,self).dispatch(*args,**kwargs)

class MustHaveExamMixin(object):
    def dispatch(self,*args,**kwargs):
        resource = self.get_resource()
        if resource.exam is None:
            return redirect(reverse('create_exam',args=(resource.pk,)))

        return super(MustHaveExamMixin,self).dispatch(*args,**kwargs)

