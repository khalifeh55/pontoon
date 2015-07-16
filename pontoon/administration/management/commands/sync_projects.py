from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from bulk_update.helper import bulk_update

from pontoon.administration.files import update_from_repository
from pontoon.administration.vcs import commit_to_vcs
from pontoon.base.models import Entity, Locale, Project, Resource, Translation
from pontoon.base.utils import match_attr
from pontoon.base.vcs_models import VCSProject


class Command(BaseCommand):
    args = '<project_slug project_slug ...>'
    help = 'Synchronize database and remote repositories.'

    def log(self, msg, *args, **kwargs):
        """Log a message to the console."""
        self.stdout.write(msg.format(*args, **kwargs))

    def info(self, msg, *args, **kwargs):
        """Log a message to the console if --verbosity=1 or more."""
        if self.verbosity >= 1:
            self.log(msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        """Log a message to the console if --verbosity=2."""
        if self.verbosity == 2:
            self.log(msg, *args, **kwargs)

    def handle(self, *args, **options):
        self.verbosity = options['verbosity']

        self.log('SYNC PROJECTS: start')
        projects = Project.objects.filter(disabled=False)
        if args:
            projects = projects.filter(slug__in=args)

        if len(projects) < 1:
            raise CommandError('No matching projects found.')

        for project in projects:
            self.handle_project(project)
        self.log('SYNC PROJECTS: done')

        # Once we've synced, we can delete all translations scheduled
        # for deletion.
        Translation.deleted_objects.all().delete()

    def handle_project(self, db_project):
        if not db_project.can_commit:
            self.log('Skipping project {0}, cannot commit to repository.'
                     .format(db_project.name))
            return

        # Pull changes from VCS and update what we know about the files.
        update_from_repository(db_project)
        vcs_project = VCSProject(db_project)
        self.update_resources(db_project, vcs_project)

        # Collect all entities across VCS and the database and get their
        # keys so we can match up matching entities.
        vcs_entities = self.get_vcs_entities(vcs_project)
        db_entities = self.get_db_entities(db_project)
        entity_keys = set().union(db_entities.keys(), vcs_entities.keys())

        changeset = ChangeSet(db_project, vcs_project)
        for key in entity_keys:
            db_entity = db_entities.get(key, None)
            vcs_entity = vcs_entities.get(key, None)
            self.handle_entity(changeset, db_project, key, db_entity, vcs_entity)

        # Apply the changeset to the files and then commit them.
        changeset.execute()
        self.commit_changes(db_project)

        # Once we've successfully committed, update the last_synced date.
        db_project.last_synced = timezone.now()
        db_project.save()

        self.log('Synced project {0}', db_project.slug)

    def handle_entity(self, changeset, db_project, key, db_entity, vcs_entity):
        """
        Determine what needs to be synced between the database and VCS versions
        of a single entity and log what needs to be changed in the changeset.
        """
        if vcs_entity is None:
            if db_entity is None:
                # This should never happen. What? Hard abort.
                raise CommandError('No entities found for key {0}'.format(key))
            else:
                # VCS no longer has the entity, remove it from Pontoon.
                changeset.obsolete_db_entity(db_entity)
        elif db_entity is None:
            # New VCS entities are added to Pontoon.
            changeset.create_db_entity(vcs_entity)
        else:
            for locale in db_project.locales.all():
                if db_entity.has_changed(locale.code):
                    # Pontoon changes overwrite whatever VCS has.
                    changeset.update_vcs_entity(locale.code, db_entity, vcs_entity)
                elif vcs_entity.has_translation_for(locale.code):
                    # If Pontoon has nothing or has not changed, and the VCS
                    # still has the entity, update Pontoon with whatever may
                    # have changed.
                    changeset.update_db_entity(locale.code, db_entity, vcs_entity)

    def update_resources(self, db_project, vcs_project):
        """Update the database on what resource files exist in VCS."""
        relative_paths = vcs_project.resources.keys()
        db_project.resource_set.exclude(path__in=relative_paths).delete()

        for relative_path, vcs_resource in vcs_project.resources.items():
            resource, created = db_project.resource_set.get_or_create(path=relative_path)
            resource.format = Resource.get_path_format(relative_path)
            resource.entity_count = len(vcs_resource.entities)
            resource.save()

    def get_vcs_entities(self, vcs_project):
        return {self.entity_key(entity): entity for entity in vcs_project.entities}

    def get_db_entities(self, db_project):
        entities = (Entity.objects
                    .select_related('resource')
                    .filter(resource__project=db_project, obsolete=False))
        return {self.entity_key(entity): entity for entity in entities}

    def entity_key(self, entity):
        """
        Generate a key for the given entity that is unique within the
        project.
        """
        return ':'.join([entity.resource.path, entity.string])

    def commit_changes(self, db_project):
        """Commit the changes we've made back to the VCS."""
        # TODO: Set translation author properly.
        user = User.objects.filter(is_superuser=True).first()

        for locale in db_project.locales.all():
            commit_message = (
                'Pontoon: Updated {locale.name} ({locale.code}) localization of '
                '{project.name}'
            ).format(locale=locale, project=db_project)

            try:
                result = commit_to_vcs(
                    db_project.repository_type,
                    db_project.locale_directory_path(locale.code),
                    commit_message,
                    user,
                    db_project.repository_url
                )
            except Exception as err:
                result = {'message': unicode(err)}

            if result is not None:
                self.log(
                    'Committing project {project.name} for {locale.name} '
                    '({locale.code}) failed: {reason}',
                    project=db_project,
                    locale=locale,
                    reason=result['message']
                )


class ChangeSet(object):
    """
    Stores a set of changes to be made to the database and the
    translations stored in VCS. Once all the necessary changes have been
    stored, execute all the changes at once efficiently.
    """
    def __init__(self, db_project, vcs_project):
        self.db_project = db_project
        self.vcs_project = vcs_project

        self.executed = False
        self.changes = {
            'update_vcs': [],
            'update_db': [],
            'obsolete_db': [],
            'create_db': []
        }

        self.entities_to_update = []
        self.translations_to_update = []
        self.translations_to_create = []

    def update_vcs_entity(self, locale_code, db_entity, vcs_entity):
        """
        Replace the translations in VCS with the translations from the
        database.
        """
        self.changes['update_vcs'].append((locale_code, db_entity, vcs_entity))

    def create_db_entity(self, vcs_entity):
        """Create a new entity in the database."""
        self.changes['create_db'].append(vcs_entity)

    def update_db_entity(self, locale_code, db_entity, vcs_entity):
        """Update the database with translations from VCS."""
        self.changes['update_db'].append((locale_code, db_entity, vcs_entity))

    def obsolete_db_entity(self, db_entity):
        """Mark the given entity as obsolete."""
        self.changes['obsolete_db'].append(db_entity.pk)

    def execute(self):
        """
        Execute the changes stored in this changeset. Execute can only
        be called once per changeset; subsequent calls raise a
        RuntimeError, even if the changes failed.
        """
        if self.executed:
            raise RuntimeError('execute() can only be called once per changeset.')
        else:
            self.executed = True

        # Store locales and resources for FK relationships.
        self.locales = {l.code: l for l in Locale.objects.all()}
        self.resources = {r.path: r for r in self.db_project.resource_set.all()}

        # Perform the changes and fill the lists for bulk creation and
        # updating.
        self.execute_update_vcs()
        self.execute_create_db()
        self.execute_update_db()
        self.execute_obsolete_db()

        # Apply the built-up changes to the DB
        if len(self.entities_to_update) > 0:
            bulk_update(self.entities_to_update, update_fields=[
                'resource',
                'string',
                'string_plural',
                'key',
                'comment',
                'order',
                'source'
            ])

        Translation.objects.bulk_create(self.translations_to_create)
        if len(self.translations_to_update) > 0:
            bulk_update(self.translations_to_update, update_fields=[
                'entity',
                'locale',
                'string',
                'plural_form',
                'approved',
                'approved_user',
                'approved_date',
                'fuzzy',
                'extra'
            ])

    def execute_update_vcs(self):
        resources = self.vcs_project.resources
        changed_resources = set()

        for locale_code, db_entity, vcs_entity in self.changes['update_vcs']:
            changed_resources.add(resources[db_entity.resource.path])

            vcs_translation = vcs_entity.translations[locale_code]
            db_translations = (db_entity.translation_set
                               .filter(approved=True, locale__code=locale_code))

            # If no DB translations are fuzzy, set fuzzy to False.
            # Otherwise, it's true.
            vcs_translation.fuzzy = any(t for t in db_translations if t.fuzzy)

            # Replace existing translations with ones from the database.
            vcs_translation.strings = {
                db.plural_form: db.string for db in db_translations
            }

        for resource in changed_resources:
            resource.save()

    def get_entity_updates(self, vcs_entity):
        """
        Return a dict of the properties and values necessary to create
        or update a database entity from a VCS entity.
        """
        return {
            'resource': self.resources[vcs_entity.resource.path],
            'string': vcs_entity.string,
            'string_plural': '',  # TODO: Support plural source.
            'key': '',  # TODO: Support keys separate from source.
            'comment': '\n'.join(vcs_entity.comments),
            'order': vcs_entity.order,
            'source': ''  # TODO: Support source
        }

    def execute_create_db(self):
        for vcs_entity in self.changes['create_db']:
            entity = Entity(**self.get_entity_updates(vcs_entity))
            entity.save()  # We can't use bulk_create since we need a PK

            for locale_code, vcs_translation in vcs_entity.translations.items():
                for plural_form, string in vcs_translation.strings.items():
                    self.translations_to_create.append(Translation(
                        entity=entity,
                        locale=self.locales[locale_code],
                        string=string,
                        plural_form=plural_form,
                        approved=True,
                        approved_date=timezone.now(),
                        fuzzy=vcs_translation.fuzzy
                    ))

    def execute_update_db(self):
        # TODO: Optimize to only update translations if necessary.
        for locale_code, db_entity, vcs_entity in self.changes['update_db']:
            for field, value in self.get_entity_updates(vcs_entity).items():
                setattr(db_entity, field, value)
            self.entities_to_update.append(db_entity)

            # Update translations for the entity.
            vcs_translation = vcs_entity.translations[locale_code]
            db_translations = db_entity.translation_set.filter(locale__code=locale_code)
            approved_translations = []

            for plural_form, string in vcs_translation.strings.items():
                # Check if we need to modify an existing translation or
                # create a new one.
                db_translation = match_attr(db_translations,
                                            plural_form=plural_form,
                                            string=string)
                if db_translation:
                    if not db_translation.approved:
                        db_translation.approved = True
                        db_translation.approved_date = timezone.now()
                    db_translation.fuzzy = vcs_translation.fuzzy
                    db_translation.extra = vcs_translation.extra

                    self.translations_to_update.append(db_translation)
                    approved_translations.append(db_translation)
                else:
                    self.translations_to_create.append(Translation(
                        entity=db_entity,
                        locale=self.locales[locale_code],
                        string=string,
                        plural_form=plural_form,
                        approved=True,
                        approved_date=timezone.now(),
                        fuzzy=vcs_translation.fuzzy,
                        extra=vcs_translation.extra
                    ))

            # Any existing translations that were not approved get unapproved.
            for translation in db_translations:
                if translation not in approved_translations:
                    translation.approved = False
                    translation.approved_user = None
                    translation.approved_date = None
                    self.translations_to_update.append(translation)

    def execute_obsolete_db(self):
        (Entity.objects
            .filter(pk__in=self.changes['obsolete_db'])
            .update(obsolete=True))
