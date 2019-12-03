#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, unicode_literals
from logging import getLogger
import Queue

import xbmcgui

from .get_metadata import GetMetadataThread
from .fill_metadata_queue import FillMetadataQueue
from .process_metadata import ProcessMetadataThread
from . import common, sections
from .. import utils, timing, backgroundthread, variables as v, app
from .. import plex_functions as PF, itemtypes

if common.PLAYLIST_SYNC_ENABLED:
    from .. import playlists


LOG = getLogger('PLEX.sync.full_sync')
# How many items will be put through the processing chain at once?
BATCH_SIZE = 250
# Size of queue for xmls to be downloaded from PMS for/and before processing
QUEUE_BUFFER = 50
# Max number of xmls held in memory
MAX_QUEUE_SIZE = 500
# Safety margin to filter PMS items - how many seconds to look into the past?
UPDATED_AT_SAFETY = 60 * 5
LAST_VIEWED_AT_SAFETY = 60 * 5


class FullSync(common.LibrarySyncMixin, backgroundthread.KillableThread):
    def __init__(self, repair, callback, show_dialog):
        """
        repair=True: force sync EVERY item
        """
        self.repair = repair
        self.callback = callback
        # For progress dialog
        self.show_dialog = show_dialog
        self.show_dialog_userdata = utils.settings('playstate_sync_indicator') == 'true'
        if self.show_dialog:
            self.dialog = xbmcgui.DialogProgressBG()
            self.dialog.create(utils.lang(39714))
        else:
            self.dialog = None

        self.section_queue = Queue.Queue()
        self.get_metadata_queue = Queue.Queue(maxsize=5000)
        self.processing_queue = backgroundthread.ProcessingQueue(maxsize=500)
        self.current_time = timing.plex_now()
        self.last_section = sections.Section()

        self.successful = True
        self.install_sync_done = utils.settings('SyncInstallRunDone') == 'true'
        self.threads = [
            GetMetadataThread(self.get_metadata_queue, self.processing_queue)
            for _ in range(int(utils.settings('syncThreadNumber')))
        ]
        for t in self.threads:
            t.start()
        super(FullSync, self).__init__()

    def update_progressbar(self, section, title, current):
        if not self.dialog:
            return
        current += 1
        try:
            progress = int(float(current) / float(section.number_of_items) * 100.0)
        except ZeroDivisionError:
            progress = 0
        self.dialog.update(progress,
                           '%s (%s)' % (section.name, section.section_type_text),
                           '%s %s/%s'
                           % (title, current, section.number_of_items))
        if app.APP.is_playing_video:
            self.dialog.close()
            self.dialog = None

    @utils.log_time
    def processing_loop_new_and_changed_items(self):
        LOG.debug('Start working')
        scanner_thread = FillMetadataQueue(self.repair,
                                           self.section_queue,
                                           self.get_metadata_queue)
        scanner_thread.start()
        process_thread = ProcessMetadataThread(self.current_time,
                                               self.processing_queue,
                                               self.update_progressbar)
        process_thread.start()
        LOG.debug('Waiting for threads to finish up')
        scanner_thread.join()
        for t in self.threads:
            t.join()
        LOG.debug('Download metadata threads finished')
        # Sentinel for the process_thread once we added everything else
        self.processing_queue.put_sentinel(sections.Section())
        process_thread.join()
        self.successful = process_thread.successful
        LOG.debug('threads finished work. successful: %s', self.successful)

    @utils.log_time
    def processing_loop_playstates(self):
        while not self.should_cancel():
            section = self.section_queue.get()
            self.section_queue.task_done()
            if section is None:
                break
            self.playstate_per_section(section)

    def playstate_per_section(self, section):
        LOG.debug('Processing %s playstates for library section %s',
                  section.number_of_items, section)
        try:
            iterator = section.iterator
            iterator = common.tag_last(iterator)
            last = True
            while not self.should_cancel():
                with section.context(self.current_time) as itemtype:
                    for last, xml_item in iterator:
                        section.count += 1
                        if not itemtype.update_userdata(xml_item, section.plex_type):
                            # Somehow did not sync this item yet
                            itemtype.add_update(xml_item,
                                                section_name=section.name,
                                                section_id=section.section_id)
                        itemtype.plexdb.update_last_sync(int(xml_item.attrib['ratingKey']),
                                                         section.plex_type,
                                                         self.current_time)
                        self.update_progressbar(section, '', section.count)
                        if section.count % (10 * BATCH_SIZE) == 0:
                            break
                if last:
                    break
        except RuntimeError:
            LOG.error('Could not entirely process section %s', section)
            self.successful = False

    def threaded_get_iterators(self, kinds, queue, all_items):
        """
        Getting iterators is costly, so let's do it asynchronously
        """
        LOG.debug('Start threaded_get_iterators')
        try:
            for kind in kinds:
                for section in (x for x in app.SYNC.sections
                                if x.section_type == kind[1]):
                    if self.should_cancel():
                        LOG.debug('Need to exit now')
                        return
                    if not section.sync_to_kodi:
                        LOG.info('User chose to not sync section %s', section)
                        continue
                    section = sections.get_sync_section(section,
                                                        plex_type=kind[0])
                    if self.repair or all_items:
                        updated_at = None
                    else:
                        updated_at = section.last_sync - UPDATED_AT_SAFETY \
                            if section.last_sync else None
                    try:
                        section.iterator = PF.get_section_iterator(
                            section.section_id,
                            plex_type=section.plex_type,
                            updated_at=updated_at,
                            last_viewed_at=None)
                    except RuntimeError:
                        LOG.error('Sync at least partially unsuccessful!')
                        LOG.error('Error getting section iterator %s', section)
                    else:
                        section.number_of_items = section.iterator.total
                        if section.number_of_items > 0:
                            self.processing_queue.add_section(section)
                            queue.put(section)
                            LOG.debug('Put section in queue: %s', section)
        except Exception:
            utils.ERROR(notify=True)
        finally:
            queue.put(None)
            LOG.debug('Exiting threaded_get_iterators')

    def full_library_sync(self):
        kinds = [
            (v.PLEX_TYPE_MOVIE, v.PLEX_TYPE_MOVIE),
            (v.PLEX_TYPE_SHOW, v.PLEX_TYPE_SHOW),
            (v.PLEX_TYPE_SEASON, v.PLEX_TYPE_SHOW),
            (v.PLEX_TYPE_EPISODE, v.PLEX_TYPE_SHOW)
        ]
        if app.SYNC.enable_music:
            kinds.extend([
                (v.PLEX_TYPE_ARTIST, v.PLEX_TYPE_ARTIST),
                (v.PLEX_TYPE_ALBUM, v.PLEX_TYPE_ARTIST),
            ])
        # ADD NEW ITEMS
        # Already start setting up the iterators. We need to enforce
        # syncing e.g. show before season before episode
        backgroundthread.KillableThread(
            target=self.threaded_get_iterators,
            args=(kinds, self.section_queue, False)).start()
        # Do the heavy lifting
        self.processing_loop_new_and_changed_items()
        common.update_kodi_library(video=True, music=True)
        if self.should_cancel() or not self.successful:
            return

        # Sync Plex playlists to Kodi and vice-versa
        if common.PLAYLIST_SYNC_ENABLED:
            if self.show_dialog:
                if self.dialog:
                    self.dialog.close()
                self.dialog = xbmcgui.DialogProgressBG()
                # "Synching playlists"
                self.dialog.create(utils.lang(39715))
            if not playlists.full_sync() or self.should_cancel():
                return

        # SYNC PLAYSTATE of ALL items (otherwise we won't pick up on items that
        # were set to unwatched). Also mark all items on the PMS to be able
        # to delete the ones still in Kodi
        LOG.debug('Start synching playstate and userdata for every item')
        if app.SYNC.enable_music:
            # In order to not delete all your songs again
            kinds.extend([
                (v.PLEX_TYPE_SONG, v.PLEX_TYPE_ARTIST),
            ])
        # Make sure we're not showing an item's title in the sync dialog
        if not self.show_dialog_userdata and self.dialog:
            # Close the progress indicator dialog
            self.dialog.close()
            self.dialog = None
        backgroundthread.KillableThread(
            target=self.threaded_get_iterators,
            args=(kinds, self.section_queue, True)).start()
        self.processing_loop_playstates()
        if self.should_cancel() or not self.successful:
            return

        # Delete movies that are not on Plex anymore
        LOG.debug('Looking for items to delete')
        kinds = [
            (v.PLEX_TYPE_MOVIE, itemtypes.Movie),
            (v.PLEX_TYPE_SHOW, itemtypes.Show),
            (v.PLEX_TYPE_SEASON, itemtypes.Season),
            (v.PLEX_TYPE_EPISODE, itemtypes.Episode)
        ]
        if app.SYNC.enable_music:
            kinds.extend([
                (v.PLEX_TYPE_ARTIST, itemtypes.Artist),
                (v.PLEX_TYPE_ALBUM, itemtypes.Album),
                (v.PLEX_TYPE_SONG, itemtypes.Song)
            ])
        for plex_type, context in kinds:
            # Delete movies that are not on Plex anymore
            while True:
                with context(self.current_time) as ctx:
                    plex_ids = list(
                        ctx.plexdb.plex_id_by_last_sync(plex_type,
                                                        self.current_time,
                                                        BATCH_SIZE))
                    for plex_id in plex_ids:
                        if self.should_cancel():
                            return
                        ctx.remove(plex_id, plex_type)
                if len(plex_ids) < BATCH_SIZE:
                    break
        LOG.debug('Done looking for items to delete')

    def run(self):
        app.APP.register_thread(self)
        LOG.info('Running library sync with repair=%s', self.repair)
        try:
            self.run_full_library_sync()
        finally:
            app.APP.deregister_thread(self)
            LOG.info('Library sync done. successful: %s', self.successful)

    @utils.log_time
    def run_full_library_sync(self):
        try:
            # Get latest Plex libraries and build playlist and video node files
            if self.should_cancel() or not sections.sync_from_pms(self):
                return
            if self.should_cancel():
                self.successful = False
                return
            self.full_library_sync()
        finally:
            common.update_kodi_library(video=True, music=True)
            if self.dialog:
                self.dialog.close()
            if not self.successful and not self.should_cancel():
                # "ERROR in library sync"
                utils.dialog('notification',
                             heading='{plex}',
                             message=utils.lang(39410),
                             icon='{error}')
            self.callback(self.successful)


def start(show_dialog, repair=False, callback=None):
    # Call run() and NOT start in order to not spawn another thread
    FullSync(repair, callback, show_dialog).run()
