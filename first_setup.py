import random
import re
import os
import traceback
import urllib.parse
from importlib import reload

def print_notice(s):
    print('\033[92m'+s+'\033[0m\n')

def path_exists(path):
    if not os.path.exists(path):
        print("That path doesn't exist")
        return False
    else:
        return True

class Question(object):
    def __init__(self, key, question, default, validation=None):
        self.key = key
        self.question = question
        self.default = default
        self.validation = validation

    def validate(self, value):
        return self.validation is None or self.validation(value)

class Command(object):

    questions = [
        Question('DEBUG', 'Is this installation for development?', False),
        Question('ALLOWED_HOSTS', 'Domain names that the site will be served from', 'numbas-lti.youruni.edu'),
        Question('DB_ENGINE', 'Which database engine are you using?', 'mysql'),
        Question('STATIC_ROOT', 'Where are static files stored?', '/srv/numbas-lti-static/', validation=path_exists),
        Question('MEDIA_ROOT', 'Where are uploaded files stored?', '/srv/numbas-lti-media/', validation=path_exists),
    ]
    db_questions = [
        Question('DB_NAME', 'Name of the database:', 'numbas_lti'),
        Question('DB_USER', 'Database user:', 'numbas_lti'),
        Question('DB_PASSWORD', 'Database password:', ''),
        Question('DB_HOST', 'Database host:', 'localhost'),
    ]

    sqlite_template = """DATABASES = {{
    'default': {{
        'ENGINE': 'django.db.backends.{DB_ENGINE}',
        'NAME': os.path.join(BASE_DIR, '{DB_NAME}'),
    }}
}}"""

    other_db_template = """DATABASES = {{
    'default': {{
        'ENGINE': 'django.db.backends.{DB_ENGINE}',
        'NAME': '{DB_NAME}',
        'USER': '{DB_USER}',
        'PASSWORD': '{DB_PASSWORD}',
        'HOST': '{DB_HOST}',
    }}
}}"""

    redis_template = """CHANNEL_LAYERS = {{
    "default": {{
        "BACKEND": "asgi_redis.RedisChannelLayer",
        "CONFIG": {{
            "hosts": [os.environ.get('REDIS_URL','redis://localhost:6379')],
        }},
        "ROUTING": "numbasltiprovider.routing.channel_routing",
    }},
}}"""

    inmemory_template = """CHANNEL_LAYERS = {{
    "default": {{
        "BACKEND": "asgiref.inmemory.ChannelLayer",
        "ROUTING": "numbasltiprovider.routing.channel_routing",
    }},
}}"""

    def __init__(self):
        self.written_files = []

    def handle(self):
        print_notice("This script will configure the Numbas LTI provider up to a point where you can open it in a web browser, based on your answers to the following questions.")

        self.get_values()

        self.write_files()

        import numbasltiprovider.settings
        reload(numbasltiprovider.settings)

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "numbasltiprovider.settings")

        print_notice("Now we'll check that everything works properly")

        self.run_management_command('check')

        if self.get_input('Would you like to automatically set up the database now?', True):
            self.run_management_command('migrate')

        import django
        django.setup()
        from django.contrib.auth.models import User
        superusers = User.objects.filter(is_superuser=True)
        if superusers.exists():
            if self.get_input("There's already at least one admin user.\nWould you like to create another admin user now?", False):
                self.run_management_command('createsuperuser')
        else:
            if self.get_input('Would you like to create an admin user now?', True):
                self.run_management_command('createsuperuser')

        print_notice("Done!")

        if self.values['DEBUG']:
            print_notice("Run\n  python manage.py runserver\nto start a development server at http://localhost:8000.")
        else:
            self.run_management_command('collectstatic')
            print_notice("The Numbas LTI provider is now set up. Once you've configured your web server, it'll be ready to use.")

    def get_values(self):
        self.values = {}

        self.values['SECRET_KEY'] =''.join(random.SystemRandom().choice('abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)') for i in range(50))
        self.values['PWD'] = os.getcwd()

        for question in self.questions:
            self.get_value(question)
            if question.key=='DB_ENGINE':
                if 'sqlite' not in self.values[question.key]:
                    for question in self.db_questions:
                        self.get_value(question)
                else:
                    self.values['DB_NAME'] = self.get_input('Name of the database file:', 'db.sqlite3')

        def enrep(value):
            rep = repr(value)
            if isinstance(value, str):
                rep = rep[1:-1]
            return rep

        self.rvalues = {key: enrep(value) for key, value in self.values.items()}

    def get_value(self, question):
        default = question.default
        if os.path.exists('numbasltiprovider/settings.py'):
            import numbasltiprovider.settings
            try:
                default = getattr(numbasltiprovider.settings, question.key)
                if isinstance(default,list):
                    default = default[0] if len(default)==1 else ''
            except AttributeError:
                if question.key=='DB_ENGINE':
                    default = numbasltiprovider.settings.DATABASES['default']['ENGINE'].replace('django.db.backends.', '')
                elif question.key[:3]=='DB_' and question.key[3:] in numbasltiprovider.settings.DATABASES['default']:
                    default = numbasltiprovider.settings.DATABASES['default'][question.key[3:]]
        self.values[question.key] = self.get_input(question.question, default, question.validation)


    def write_files(self):

        def set_database(m, rvalues):
            template = self.sqlite_template if 'sqlite' in rvalues['DB_ENGINE'] else self.other_db_template
            return template.format(**rvalues)

        def set_channel_layers(m, rvalues):
            template = self.inmemory_template if self.values['DEBUG'] else self.redis_template
            return template.format(**rvalues)

        settings_subs = [
            (r"^DEBUG = (False)", 'DEBUG'),
            (r"^STATIC_ROOT = '(static/)'", 'STATIC_ROOT'),
            (r"^MEDIA_ROOT = '(media/)'", 'MEDIA_ROOT'),
            (r"^DATABASES = {.*?^}", set_database),
            (r"^CHANNEL_LAYERS = {.*?^}", set_channel_layers),
            (r"^SECRET_KEY = '()'", 'SECRET_KEY'),
            (r"^ALLOWED_HOSTS = \['(localhost)'\]", 'ALLOWED_HOSTS'),
        ]
        self.sub_file('numbasltiprovider/settings.py', settings_subs)

        if len(self.written_files):
            print_notice("The following files have been written. You should look at them now to see if you need to make any more changes.")
            for f in self.written_files:
                print_notice(' * '+f)
            print('')

    def sub_file(self, fname, subs):
        if os.path.exists(fname):
            overwrite = self.get_input("{} already exists. Overwrite it?".format(fname), False)
            if not overwrite:
                return

        self.written_files.append(fname)

        with open(fname+'.dist') as f:
            text = f.read()

        for pattern, key in subs:
            pattern = re.compile(pattern, re.MULTILINE | re.DOTALL)
            if callable(key):
                text = self.sub_fn(text, pattern, key)
            else:
                text = self.sub(text, pattern, self.rvalues[key])


        with open(fname, 'w') as f:
            f.write(text)

    def sub_fn(self, source, pattern, fn):
        m = pattern.search(source)
        if not m:
            print(source)
            raise Exception("Didn't find {}".format(pattern.pattern))
        start, end = m.span(0)
        out = fn(m, self.rvalues)
        return source[:start]+out+source[end:]

    def sub(self, source, pattern, value):
        def fix(m):
            t = m.group(0)
            start, end = m.span(1)
            ts, te = m.span(0)
            start -= ts
            end -= ts
            return t[:start]+value+t[end:]
        if not pattern.search(source):
            raise Exception("Didn't find {}".format(pattern.pattern))
        return pattern.sub(fix, source)

    def run_management_command(self, *args):
        from django.core.management import ManagementUtility
        args = ['manage.py'] + list(args)
        utility = ManagementUtility(args)
        try:
            utility.execute()
        except SystemExit:
            pass
        print('')

    def get_input(self, question, default, validation=None):
        v = None
        try:
            while v is None:
                if isinstance(default, bool):
                    if default is not None:
                        q = question+(' [Y/n]' if default else ' [y/N]')
                    else:
                        q = question
                    t = input(q+' ').strip().lower()
                    if t=='' and default is not None:
                        v = default
                    if t=='y':
                        v = True
                    if t=='n':
                        v = False
                else:
                    if default is not None:
                        q = "{} ['{}']".format(question, str(default))
                    else:
                        q = question
                    t = input(q+' ').strip()
                    if t=='' and default is not None:
                        v = default
                    if t:
                        v = t
                if validation is not None and not validation(v):
                    v = None
        except KeyboardInterrupt:
            print('')
            raise SystemExit
        print('')
        return v
            

if __name__ == '__main__':
    command = Command()
    try:
        command.handle()
    except Exception as e:
        traceback.print_exc()
        print_notice("The setup script failed. Look at the error message above for a description of why.")
