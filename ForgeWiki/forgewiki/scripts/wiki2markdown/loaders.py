#       Licensed to the Apache Software Foundation (ASF) under one
#       or more contributor license agreements.  See the NOTICE file
#       distributed with this work for additional information
#       regarding copyright ownership.  The ASF licenses this file
#       to you under the Apache License, Version 2.0 (the
#       "License"); you may not use this file except in compliance
#       with the License.  You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#       Unless required by applicable law or agreed to in writing,
#       software distributed under the License is distributed on an
#       "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#       KIND, either express or implied.  See the License for the
#       specific language governing permissions and limitations
#       under the License.

import logging
import os
import json
import datetime
from pylons import tmpl_context as c
from ming.orm.ormsession import ThreadLocalORMSession

from allura import model as M
from forgewiki import model as WM
from forgewiki.converters import mediawiki2markdown
from forgewiki.converters import mediawiki_internal_links2markdown
from allura.lib import helpers as h
from allura.lib import utils
from allura.model.session import artifact_orm_session

log = logging.getLogger(__name__)


class MediawikiLoader(object):

    """Load MediaWiki data from json to Allura wiki tool"""
    TIMESTAMP_FMT = '%Y%m%d%H%M%S'

    def __init__(self, options):
        self.options = options
        self.nbhd = M.Neighborhood.query.get(name=options.nbhd)
        if not self.nbhd:
            raise ValueError("Can't find neighborhood with name %s"
                             % options.nbhd)
        self.project = M.Project.query.get(shortname=options.project,
                                           neighborhood_id=self.nbhd._id)
        if not self.project:
            raise ValueError("Can't find project with shortname %s "
                             "and neighborhood_id %s"
                             % (options.project, self.nbhd._id))

        self.wiki = self.project.app_instance('wiki')
        if not self.wiki:
            raise ValueError("Can't find wiki app in given project")

        h.set_context(self.project.shortname, 'wiki', neighborhood=self.nbhd)

    def load(self):
        try:
            self.project.notifications_disabled = True
            artifact_orm_session._get().skip_mod_date = True
            self.load_pages()
            ThreadLocalORMSession.flush_all()
            log.info('Loading wiki done')
        finally:
            self.project.notifications_disabled = False
            artifact_orm_session._get().skip_mod_date = False

    def _pages(self):
        """Yield path to page dump directory for next wiki page"""
        pages_dir = os.path.join(self.options.dump_dir, 'pages')
        pages = []
        if not os.path.isdir(pages_dir):
            return
        pages = os.listdir(pages_dir)
        for directory in pages:
            dir_path = os.path.join(pages_dir, directory)
            if os.path.isdir(dir_path):
                yield dir_path

    def _history(self, page_dir):
        """Yield page_data for next wiki page in edit history"""
        page_dir = os.path.join(page_dir, 'history')
        if not os.path.isdir(page_dir):
            return
        pages = os.listdir(page_dir)
        pages.sort()  # ensure that history in right order
        for page in pages:
            fn = os.path.join(page_dir, page)
            try:
                with open(fn, 'r') as pages_file:
                    page_data = json.load(pages_file)
            except IOError, e:
                log.error("Can't open file: %s", str(e))
                raise
            except ValueError, e:
                log.error("Can't load data from file %s: %s", fn, str(e))
                raise
            yield page_data

    def _talk(self, page_dir):
        """Return talk data from json dump"""
        filename = os.path.join(page_dir, 'discussion.json')
        if not os.path.isfile(filename):
            return
        try:
            with open(filename, 'r') as talk_file:
                talk_data = json.load(talk_file)
        except IOError, e:
            log.error("Can't open file: %s", str(e))
            raise
        except ValueError, e:
            log.error("Can't load data from file %s: %s", filename, str(e))
            raise
        return talk_data

    def _attachments(self, page_dir):
        """Yield (filename, full path) to next attachment for given page."""
        attachments_dir = os.path.join(page_dir, 'attachments')
        if not os.path.isdir(attachments_dir):
            return
        attachments = os.listdir(attachments_dir)
        for filename in attachments:
            yield filename, os.path.join(attachments_dir, filename)

    def load_pages(self):
        """Load pages with edit history from json to Allura wiki tool"""
        log.info('Loading pages into allura...')
        for page_dir in self._pages():
            for page in self._history(page_dir):
                p = WM.Page.upsert(page['title'])
                p.viewable_by = ['all']
                p.text = mediawiki_internal_links2markdown(
                    mediawiki2markdown(page['text']),
                    page['title'])
                timestamp = datetime.datetime.strptime(page['timestamp'],
                                                       self.TIMESTAMP_FMT)
                p.mod_date = timestamp
                c.user = (M.User.query.get(username=page['username'].lower())
                          or M.User.anonymous())
                ss = p.commit()
                ss.mod_date = ss.timestamp = timestamp

            # set home to main page
            if page['title'] == 'Main_Page':
                gl = WM.Globals.query.get(app_config_id=self.wiki.config._id)
                if gl is not None:
                    gl.root = page['title']
            log.info('Loaded history of page %s (%s)',
                     page['page_id'], page['title'])

            self.load_talk(page_dir, page['title'])
            self.load_attachments(page_dir, page['title'])

    def load_talk(self, page_dir, page_title):
        """Load talk for page.

        page_dir - path to directory with page dump.
        page_title - page title in Allura Wiki
        """
        talk_data = self._talk(page_dir)
        if not talk_data:
            return
        text = mediawiki2markdown(talk_data['text'])
        page = WM.Page.query.get(app_config_id=self.wiki.config._id,
                                 title=page_title)
        if not page:
            return
        thread = M.Thread.query.get(ref_id=page.index_id())
        if not thread:
            return
        timestamp = datetime.datetime.strptime(talk_data['timestamp'],
                                               self.TIMESTAMP_FMT)
        c.user = (M.User.query.get(username=talk_data['username'].lower())
                  or M.User.anonymous())
        thread.add_post(
            text=text,
            discussion_id=thread.discussion_id,
            thread_id=thread._id,
            timestamp=timestamp,
            ignore_security=True)
        log.info('Loaded talk for page %s', page_title)

    def load_attachments(self, page_dir, page_title):
        """Load attachments for page.

        page_dir - path to directory with page dump.
        """
        page = WM.Page.query.get(app_config_id=self.wiki.config._id,
                                 title=page_title)
        for filename, path in self._attachments(page_dir):
            try:
                with open(path) as fp:
                    page.attach(filename, fp,
                                content_type=utils.guess_mime_type(filename))
            except IOError, e:
                log.error("Can't open file: %s", str(e))
                raise
        log.info('Loaded attachments for page %s.', page_title)
