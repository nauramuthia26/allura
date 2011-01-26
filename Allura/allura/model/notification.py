'''Manage notifications and subscriptions

When an artifact is modified:
- Notification generated by tool app
- Search is made for subscriptions matching the notification
- Notification is added to each matching subscriptions' queue

Periodically:
- For each subscriptions with notifications and direct delivery:
   - For each notification, enqueue as a separate email message
   - Clear subscription's notification list
- For each subscription with notifications and delivery due:
   - Enqueue one email message with all notifications
   - Clear subscription's notification list

Notifications are also available for use in feeds
'''

import logging
from datetime import datetime, timedelta
from collections import defaultdict

from pylons import c, g
from tg import config
import pymongo

from ming import schema as S
from ming.orm import MappedClass, FieldProperty, ForeignIdProperty, RelationProperty, session

from allura.lib import helpers as h

from .session import main_orm_session, project_orm_session
from .types import ArtifactReferenceType


log = logging.getLogger(__name__)

MAILBOX_QUIESCENT=None # Re-enable with [#1384]: timedelta(minutes=10)

class Notification(MappedClass):
    class __mongometa__:
        session = main_orm_session
        name = 'notification'

    _id = FieldProperty(str, if_missing=h.gen_message_id)

    # Classify notifications
    project_id = ForeignIdProperty('Project', if_missing=lambda:c.project._id)
    app_config_id = ForeignIdProperty('AppConfig', if_missing=lambda:c.app.config._id)
    artifact_reference = FieldProperty(ArtifactReferenceType)
    topic = FieldProperty(str)

    # Notification Content
    in_reply_to=FieldProperty(str)
    from_address=FieldProperty(str)
    reply_to_address=FieldProperty(str)
    subject=FieldProperty(str)
    text=FieldProperty(str)
    link=FieldProperty(str)
    author_id=ForeignIdProperty('User')
    feed_meta=FieldProperty(dict(
            link=str,
            created=S.DateTime(if_missing=datetime.utcnow),
            unique_id=S.String(if_missing=lambda:h.nonce(40)),
            author_name=S.String(if_missing=lambda:c.user.display_name),
            author_link=S.String(if_missing=lambda:c.user.url())))

    @classmethod
    def post(cls, artifact, topic, **kw):
        '''Create a notification and  send the notify message'''
        n = cls._make_notification(artifact, topic, **kw)
        idx = artifact.index()
        g.publish('react', 'forgemail.notify', dict(
                notification_id=n._id,
                artifact_index_id=idx['id'],
                topic=topic))
        return n

    @classmethod
    def post_user(cls, user, artifact, topic, **kw):
        '''Create a notification and deliver directly to a user's flash
    mailbox'''
        try:
            mbox = Mailbox(user_id=user._id, is_flash=True,
                           project_id=None,
                           app_config_id=None)
            session(mbox).flush(mbox)
        except pymongo.errors.DuplicateKeyError:
            session(mbox).expunge(mbox)
            mbox = Mailbox.query.get(user_id=user._id, is_flash=True)
        n = cls._make_notification(artifact, topic, **kw)
        mbox.queue.append(n._id)
        return n

    @classmethod
    def _make_notification(cls, artifact, topic, **kwargs):
        idx = artifact.index()
        subject_prefix = '[%s:%s] ' % (
            c.project.shortname, c.app.config.options.mount_point)
        if topic == 'message':
            post = kwargs.pop('post')
            subject = post.subject or ''
            if post.parent_id and not subject.lower().startswith('re:'):
                subject = 'Re: ' + subject
            author = post.author()
            d = dict(
                _id=post._id,
                from_address=str(author._id),
                reply_to_address='"%s" <%s>' % (
                    subject_prefix, getattr(artifact, 'email_address', 'noreply@in.sf.net')),
                subject=subject_prefix + subject,
                text=post.text,
                in_reply_to=post.parent_id)
        else:
            subject = kwargs.pop('subject', '%s modified by %s' % (
                    idx['title_s'], c.user.display_name))
            d = dict(
                from_address='%s <%s>' % (
                    idx['title_s'], artifact.email_address),
                reply_to_address='"%s" <%s>' % (
                    idx['title_s'], artifact.email_address),
                subject=subject_prefix + subject,
                text=kwargs.pop('text', subject))
        if not d.get('text'):
            d['text'] = ''
        assert d['reply_to_address'] is not None
        n = cls(artifact_reference=artifact.dump_ref(),
                topic=topic,
                link=kwargs.pop('link', artifact.url()),
                **d)
        return n

    def footer(self):
        prefix = config.get('forgemail.url', 'https://sourceforge.net')
        return '''

---

Sent from sourceforge.net because you indicated interest in <%s%s>

To unsubscribe from further messages, please visit <%s/auth/prefs/>
''' % (prefix, self.link, prefix)

    def send_direct(self, user_id):
        g.publish('audit', 'forgemail.send_email', {
                'destinations':[str(user_id)],
                'from':self.from_address,
                'reply_to':self.reply_to_address,
                'subject':self.subject,
                'message_id':self._id,
                'in_reply_to':self.in_reply_to,
                'text':(self.text or '') + self.footer()},
                  serializer='pickle')

    @classmethod
    def send_digest(self, user_id, from_address, subject, notifications,
                    reply_to_address=None):
        if not notifications: return
        if reply_to_address is None:
            reply_to_address = from_address
        text = [ 'Digest of %s' % subject ]
        for n in notifications:
            text.append('From: %s' % n.from_address)
            text.append('Subject: %s' % (n.subject or '(no subject)'))
            text.append('Message-ID: %s' % n._id)
            text.append('')
            text.append(n.text or '-no text-')
        text.append(n.footer())
        text = '\n'.join(text)
        g.publish('audit', 'forgemail.send_email', {
                'destinations':[str(user_id)],
                'from':from_address,
                'reply_to':reply_to_address,
                'subject':subject,
                'message_id':h.gen_message_id(),
                'text':text},
                  serializer='pickle')

    @classmethod
    def send_summary(self, user_id, from_address, subject, notifications):
        if not notifications: return
        text = [ 'Digest of %s' % subject ]
        for n in notifications:
            text.append('From: %s' % n.from_address)
            text.append('Subject: %s' % (n.subject or '(no subject)'))
            text.append('Message-ID: %s' % n._id)
            text.append('')
            text.append(h.text.truncate(n.text or '-no text-', 128))
        text.append(n.footer())
        text = '\n'.join(text)
        g.publish('audit', 'forgemail.send_email', {
                'destinations':[str(user_id)],
                'from':from_address,
                'reply_to':from_address,
                'subject':subject,
                'message_id':h.gen_message_id(),
                'text':text},
                  serializer='pickle')

class Mailbox(MappedClass):
    class __mongometa__:
        session = main_orm_session
        name = 'mailbox'
        unique_indexes = [
            ('user_id', 'project_id', 'app_config_id',
             'artifact_index_id', 'topic', 'is_flash'),
            ]
        indexes = [
            ('project_id', 'artifact_index_id') ]
    _id = FieldProperty(S.ObjectId)
    user_id = ForeignIdProperty('User', if_missing=lambda:c.user._id)
    project_id = ForeignIdProperty('Project', if_missing=lambda:c.project._id)
    app_config_id = ForeignIdProperty('AppConfig', if_missing=lambda:c.app.config._id)

    # Subscription filters
    artifact_title = FieldProperty(str)
    artifact_url = FieldProperty(str)
    artifact_index_id = FieldProperty(str)
    topic = FieldProperty(str)

    # Subscription type
    is_flash = FieldProperty(bool, if_missing=False)
    type = FieldProperty(S.OneOf, 'direct', 'digest', 'summary', 'flash')
    frequency = FieldProperty(dict(
            n=int,unit=S.OneOf('day', 'week', 'month')))
    next_scheduled = FieldProperty(datetime, if_missing=datetime.utcnow)

    # Actual notification IDs
    last_modified = FieldProperty(datetime, if_missing=datetime(2000,1,1))
    queue = FieldProperty([str])

    project = RelationProperty('Project')
    app_config = RelationProperty('AppConfig')

    @classmethod
    def subscribe(
        cls,
        user_id=None, project_id=None, app_config_id=None,
        artifact=None, topic=None,
        type='direct', n=1, unit='day'):
        if user_id is None: user_id = c.user._id
        if project_id is None: project_id = c.project._id
        if app_config_id is None: app_config_id = c.app.config._id
        tool_already_subscribed = cls.query.get(user_id=user_id,
            project_id=project_id,
            app_config_id=app_config_id,
            artifact_index_id=None)
        if tool_already_subscribed:
            log.warning('Tried to subscribe to artifact %s, while there is a tool subscription', artifact)
            return
        if artifact is None:
            artifact_title = 'All artifacts'
            artifact_url = None
            artifact_index_id = None
        else:
            i = artifact.index()
            artifact_title = i['title_s']
            artifact_url = artifact.url()
            artifact_index_id = i['id']
            artifact_already_subscribed = cls.query.get(user_id=user_id,
                project_id=project_id,
                app_config_id=app_config_id,
                artifact_index_id=artifact_index_id)
            if artifact_already_subscribed:
                return
        d = dict(user_id=user_id, project_id=project_id, app_config_id=app_config_id,
                 artifact_index_id=artifact_index_id, topic=topic)
        sess = session(cls)
        try:
            mbox = cls(
                type=type, frequency=dict(n=n, unit=unit),
                artifact_title=artifact_title,
                artifact_url=artifact_url,
                **d)
            sess.flush(mbox)
        except pymongo.errors.DuplicateKeyError:
            sess.expunge(mbox)
            mbox = cls.query.get(**d)
            mbox.artifact_title = artifact_title
            mbox.artifact_url = artifact_url
            mbox.type = type
            mbox.frequency.n = n
            mbox.frequency.unit = unit
            sess.flush(mbox)
        if not artifact_index_id:
            # Unsubscribe from individual artifacts when subscribing to the tool
            for other_mbox in cls.query.find(dict(
                user_id=user_id, project_id=project_id, app_config_id=app_config_id)):
                if other_mbox is not mbox:
                    other_mbox.delete()

    @classmethod
    def unsubscribe(
        cls,
        user_id=None, project_id=None, app_config_id=None,
        artifact_index_id=None, topic=None):
        if user_id is None: user_id = c.user._id
        if project_id is None: project_id = c.project._id
        if app_config_id is None: app_config_id = c.app.config._id
        cls.query.remove(dict(
                user_id=user_id,
                project_id=project_id,
                app_config_id=app_config_id,
                artifact_index_id=artifact_index_id,
                topic=topic))

    @classmethod
    def subscribed(
        cls, user_id=None, project_id=None, app_config_id=None,
        artifact=None, topic=None):
        if user_id is None: user_id = c.user._id
        if project_id is None: project_id = c.project._id
        if app_config_id is None: app_config_id = c.app.config._id
        if artifact is None:
            artifact_index_id = None
        else:
            i = artifact.index()
            artifact_index_id = i['id']
        return cls.query.find(dict(
                user_id=user_id,
                project_id=project_id,
                app_config_id=app_config_id,
                artifact_index_id=artifact_index_id)).count() != 0

    @classmethod
    def deliver(cls, nid, artifact_index_id, topic):
        '''Called in the notification message handler to deliver notification IDs
        to the appropriate  mailboxes.  Atomically appends the nids
        to the appropriate mailboxes.
        '''
        d = {
            'project_id':c.project._id,
            'app_config_id':c.app.config._id,
            'artifact_index_id':{'$in':[None, artifact_index_id]},
            'topic':{'$in':[None, topic]}
            }
        for mbox in cls.query.find(d):
            mbox.query.update(
                dict(_id=mbox._id),
                {'$push':dict(queue=nid),
                 '$set':dict(last_modified=datetime.utcnow())})
            # Make sure the mbox doesn't stick around to be flush()ed
            session(mbox).expunge(mbox)

    @classmethod
    def fire_ready(cls):
        '''Fires all direct subscriptions with notifications as well as
        all summary & digest subscriptions with notifications that are ready
        '''
        now = datetime.utcnow()
        # Queries to find all matching subscription objects
        q_direct = dict(
            type='direct',
            queue={'$ne':[]})
        if MAILBOX_QUIESCENT:
            q_direct['last_modified']={'$lt':now - MAILBOX_QUIESCENT}
        q_digest = dict(
            type={'$in': ['digest', 'summary']},
            next_scheduled={'$lt':now})
        for mbox in cls.query.find(q_direct):
            mbox = mbox.query.find_and_modify(
                query=dict(_id=mbox._id),
                update={'$set': dict(
                        queue=[])})
            mbox.fire(now)
        for mbox in cls.query.find(q_digest):
            next_scheduled = now
            if mbox.frequency.unit == 'day':
                next_scheduled += timedelta(days=mbox.frequency.n)
            elif mbox.frequency.unit == 'week':
                next_scheduled += timedelta(days=7 * mbox.frequency.n)
            elif mbox.frequency.unit == 'month':
                next_scheduled += timedelta(days=30 * mbox.frequency.n)
            mbox = mbox.query.find_and_modify(
                query=dict(_id=mbox._id),
                update={'$set': dict(
                        next_scheduled=next_scheduled,
                        queue=[])})
            mbox.fire(now)

    def fire(self, now):
        notifications = Notification.query.find(dict(_id={'$in':self.queue}))
        if self.type == 'direct':
            ngroups = defaultdict(list)
            for n in notifications:
                if n.topic == 'message':
                    n.send_direct(self.user_id)
                    # Messages must be sent individually so they can be replied
                    # to individually
                else:
                    key = (n.subject, n.from_address, n.reply_to_address, n.author_id)
                    ngroups[key].append(n)
            # Accumulate messages from same address with same subject
            for (subject, from_address, reply_to_address, author_id), ns in ngroups.iteritems():
                if len(ns) == 1:
                    n.send_direct(self.user_id)
                else:
                    Notification.send_digest(
                        self.user_id, from_address, subject, ns, reply_to_address)
        elif self.type == 'digest':
            Notification.send_digest(
                self.user_id, 'noreply@in.sf.net', 'Digest Email',
                notifications)
        elif self.type == 'summary':
            Notification.send_summary(
                self.user_id, 'noreply@in.sf.net', 'Digest Email',
                notifications)
