# coding: utf-8
import os
import logging

import sublime
from sublime_plugin import WindowCommand, TextCommand, EventListener

from .util import abbreviate_dir, find_repo_dir, find_cwd
from .util import find_or_create_view, find_view, write_view, ensure_writeable
from .util import noop, maybe_int, get_setting
from .cmd import GitCmd
from .helpers import GitStatusHelper, GitRemoteHelper, GitStashHelper


logger = logging.getLogger(__name__)

GOTO_DEFAULT = 'file:1'

GIT_STATUS_VIEW_TITLE = '*git-status*'
GIT_STATUS_VIEW_SYNTAX = 'Packages/SublimeGit/SublimeGit Status.tmLanguage'
GIT_STATUS_VIEW_SETTINGS = {
    'translate_tabs_to_spaces': False,
    'draw_white_space': 'none',
    'word_wrap': False,
    'git_status': True,
}

STASHES = "stashes"
UNTRACKED_FILES = "untracked_files"
UNSTAGED_CHANGES = "unstaged_changes"
STAGED_CHANGES = "staged_changes"

CHANGES = "changes"  # pseudo-section to ignore staging area

SECTIONS = {
    STASHES: 'Stashes:\n',
    UNTRACKED_FILES: 'Untracked files:\n',
    UNSTAGED_CHANGES: 'Unstaged changes:\n',
    STAGED_CHANGES: 'Staged changes:\n',
    CHANGES: 'Changes:\n',
}

SECTION_SELECTOR_PREFIX = 'meta.git-status.'

STATUS_LABELS = {
    ' ': 'Unmodified',
    'M': 'Modified  ',
    'A': 'Added     ',
    'D': 'Deleted   ',
    'R': 'Renamed   ',
    'C': 'Copied    ',
    'U': 'Unmerged  ',
    '?': 'Untracked ',
    '!': 'Ignored   '
}

GIT_WORKING_DIR_CLEAN = "Nothing to commit (working directory clean)"

GIT_STATUS_HELP = """
# Movement:
#    r = refresh status
#    1-5 = jump to section
#    n = next item, N = next section
#    p = previous item, P = previous section
#
# Staging:
#    s = stage file/section, S = stage all unstaged files
#    super+k s = stage all unstaged and untracked files
#    u = unstage file/section, U = unstage all files
#    k = discard file/section, K = discard everything
#
# Other:
#    c = commit, C = commit -a (add unstaged)
#    enter = open file
#    d = view diff
#
# Stashes:
#    a = apply stash, A = pop stash
#    z = create stash from worktree"""


class GitStatusWindowCmd(GitCmd, GitStatusHelper, GitRemoteHelper, GitStashHelper):

    def build_status(self):
        branch = self.get_current_branch()
        remote = self.get_remote(branch)
        remote_url = self.get_remote_url(remote)

        abbrev_dir = abbreviate_dir(find_repo_dir(self.get_cwd()))

        head_rc, head = self.git(['log', '--max-count=1', '--abbrev-commit', '--pretty=oneline'])

        status = ""
        if remote:
            status += "Remote:   %s @ %s\n" % (remote, remote_url)
        status += "Local:    %s %s\n" % (branch if branch else '(no branch)', abbrev_dir)
        status += "Head:     %s\n" % ("nothing committed (yet)" if head_rc != 0 else head)
        status += "\n"

        # update index
        self.git_exit_code(['update-index', '--refresh'])

        status += self.build_stashes()
        status += self.build_files_status()

        if get_setting('git_show_status_help', True):
            status += GIT_STATUS_HELP

        return status

    def build_stashes(self):
        status = ""

        stashes = self.get_stashes()
        if stashes:
            status += SECTIONS[STASHES]
            for name, title in stashes:
                status += "\t%s: %s\n" % (name, title)
            status += "\n"

        return status

    def build_files_status(self):
        status = ""
        untracked, unstaged, staged = self.get_files_status()

        if not untracked and not unstaged and not staged:
            status += GIT_WORKING_DIR_CLEAN + "\n"

        # untracked files
        if untracked:
            status += SECTIONS[UNTRACKED_FILES]
            for s, f in untracked:
                status += "\t%s\n" % f.strip()
            status += "\n"

        # unstaged changes
        if unstaged:
            status += SECTIONS[UNSTAGED_CHANGES] if staged else SECTIONS[CHANGES]
            for s, f in unstaged:
                status += "\t%s %s\n" % (STATUS_LABELS[s], f)
            status += "\n"

        # staged changes
        if staged:
            status += SECTIONS[STAGED_CHANGES]
            for s, f in staged:
                status += "\t%s %s\n" % (STATUS_LABELS[s], f)
            status += "\n"

        return status


class GitStatusCommand(WindowCommand, GitStatusWindowCmd):

    def run(self):
        status = self.build_status()

        view = find_or_create_view(self.window, GIT_STATUS_VIEW_TITLE,
                                    syntax=GIT_STATUS_VIEW_SYNTAX,
                                    settings=GIT_STATUS_VIEW_SETTINGS,
                                    scratch=True,
                                    read_only=True)
        with ensure_writeable(view):
            write_view(view, status)
        self.window.focus_view(view)
        self.window.run_command('git_status_move', {'goto': 'file:1'})


class GitStatusRefreshCommand(WindowCommand, GitStatusWindowCmd):

    def run(self, goto=None, focus=True):
        view = find_view(self.window, GIT_STATUS_VIEW_TITLE)
        if view:
            status = self.build_status()
            with ensure_writeable(view):
                write_view(view, status)
            if focus:
                self.window.focus_view(view)
                if goto:
                    self.window.run_command('git_status_move', {'goto': goto})
                else:
                    self.window.run_command('git_status_move', {'goto': GOTO_DEFAULT})


class GitStatusEventListener(EventListener):

    def on_activated(self, view):
        if view.name() == GIT_STATUS_VIEW_TITLE:
            goto = None
            if view.sel():
                goto = "point:%s" % view.sel()[0].begin()
            view.window().run_command('git_status_refresh', {'goto': goto})


class GitStatusBarEventListener(EventListener, GitCmd):

    INTERVAL = 4000

    def __init__(self):
        self.running = False
        self.persistent = get_setting('git_persistent_status_bar_message', False)

    def on_activated(self, view):
        self.set_status(view)

    def on_load(self, view):
        self.set_status(view)

    def set_status(self, view):
        repo = find_repo_dir(find_cwd(view.window()))
        if repo:
            branch = self.git_string(['symbolic-ref', '-q', 'HEAD'], cwd=repo)
            branch = branch[11:] if branch.startswith('refs/heads/') else None
            self.msg = 'On branch %s' % branch if branch else 'Detached HEAD'

            if not self.persistent or not self.running:
                sublime.set_timeout(self.update_status, 0)
                self.running = True

    def update_status(self):
        logger.debug("Updating status line: %s", self.msg)
        sublime.status_message(self.msg)
        if self.persistent:
            sublime.set_timeout(self.update_status, self.INTERVAL)


class GitQuickStatusCommand(WindowCommand, GitCmd):

    def run(self):
        status = self.git_lines(['status', '--porcelain', '--untracked-files=all'])
        if not status:
            status = [GIT_WORKING_DIR_CLEAN]

        def on_done(idx):
            if idx == -1 or status[idx] == GIT_WORKING_DIR_CLEAN:
                return
            state, filename = status[idx][0:2], status[idx][3:]
            index, worktree = state
            if state == '??':
                sublime.error_message("Cannot show diff for untracked files.")
                return

            window = self.window
            if worktree != ' ':
                window.run_command('git_diff', {'path': filename})
            if index != ' ':
                window.run_command('git_diff', {'path': filename, 'cached': True})

        self.window.show_quick_panel(status, on_done, sublime.MONOSPACE_FONT)


class GitStatusTextCmd(GitCmd):

    def run(self, edit, *args):
        sublime.error_message("Unimplemented!")

    # status update
    def update_status(self, goto=None):
        self.view.window().run_command('git_status_refresh', {'goto': goto})

    # selection commands
    def get_first_point(self):
        sels = self.view.sel()
        if sels:
            return sels[0].begin()

    def get_all_points(self):
        sels = self.view.sel()
        return [s.begin() for s in sels]

    # line helpers
    def get_selected_lines(self):
        sels = self.view.sel()
        selected_lines = []
        for selection in sels:
            lines = self.view.lines(selection)
            for line in lines:
                if self.view.score_selector(line.begin(), 'meta.git-status.line') > 0:
                    selected_lines.append(line)
        return selected_lines

    # stash helpers
    def get_all_stash_regions(self):
        return self.view.find_by_selector('meta.git-status.stash.name')

    def get_all_stashes(self):
        stashes = self.get_all_stash_regions()
        return [(self.view.substr(s), self.view.substr(self.view.line(s)).strip()) for s in stashes]

    def get_selected_stashes(self):
        stashes = []
        lines = self.get_selected_lines()

        if lines:
            for s in self.get_all_stash_regions():
                for l in lines:
                    if l.contains(s):
                        name = self.view.substr(s)
                        title = self.view.substr(self.view.line(s)).strip()
                        stashes.append((name, title))
        return stashes

    # file helpers
    def get_all_file_regions(self):
        return self.view.find_by_selector('meta.git-status.file')

    def get_all_files(self):
        files = self.get_all_file_regions()
        return [(self.section_at_region(f), self.view.substr(f)) for f in files]

    def get_selected_file_regions(self):
        files = []
        lines = self.get_selected_lines()

        if not lines:
            return files

        for f in self.get_all_file_regions():
            for l in lines:
                if l.contains(f):
                    # check for renamed
                    linestr = self.view.substr(l).strip()
                    if linestr.startswith(STATUS_LABELS['R']) and ' -> ' in linestr:
                        names = self.view.substr(f)
                        # find position of divider
                        e = names.find(' -> ')
                        s = e + 4
                        # add both files
                        f1 = sublime.Region(f.begin(), f.begin() + e)
                        f2 = sublime.Region(f.begin() + s, f.end())
                        files.append((self.section_at_region(f), f1))
                        files.append((self.section_at_region(f), f2))
                    else:
                        files.append((self.section_at_region(f), f))

        return files

    def get_selected_files(self):
        return [(s, self.view.substr(f)) for s, f in self.get_selected_file_regions()]

    def get_status_lines(self):
        lines = []
        chunks = self.view.find_by_selector('meta.git-status.line')
        for c in chunks:
            lines.extend(self.view.lines(c))
        return lines

    # section helpers
    def get_sections(self):
        sections = self.view.find_by_selector('constant.other.git-status.header')
        return sections

    def section_at_point(self, point):
        for s in SECTIONS.keys():
            if self.view.score_selector(point, SECTION_SELECTOR_PREFIX + s) > 0:
                return s

    def section_at_region(self, region):
        return self.section_at_point(region.begin())

    # goto helpers
    def logical_goto_next_file(self):
        goto = "file:1"
        files = self.get_selected_files()
        if files:
            section, filename = files[-1]
            goto = "file:%s:%s" % (filename, section)
        return goto

    def logical_goto_next_stash(self):
        goto = "stash:1"
        stashes = self.get_selected_stashes()
        if stashes:
            goto = "stash:%s:stashes" % (stashes[-1][0])
        return goto


class GitStatusMoveCommand(TextCommand, GitStatusTextCmd):

    def run(self, edit, goto="file:1"):
        what, which, where = self.parse_goto(goto)
        if what == "section":
            self.move_to_section(which, where)
        elif what == "item":
            self.move_to_item(which, where)
        elif what == "file":
            self.move_to_file(which, where)
        elif what == "stash":
            self.move_to_stash(which, where)
        elif what == "point":
            point = maybe_int(which)
            if point:
                self.move_to_point(point)

    def parse_goto(self, goto):
        what, which, where = None, None, None
        parts = goto.split(':')
        what = parts[0]
        if len(parts) > 1:
            which = maybe_int(parts[1])
        if len(parts) > 2:
            where = maybe_int(parts[2])
        return (what, which, where)

    def move_to_point(self, point):
        self.view.show(point)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(point))

    def move_to_region(self, region):
        self.move_to_point(self.view.line(region).begin())

    def prev_region(self, regions, point):
        before = [r for r in regions if self.view.line(r).end() < point]
        return before[-1] if before else regions[-1]

    def next_region(self, regions, point):
        after = [r for r in regions if self.view.line(r).begin() > point]
        return after[0] if after else regions[0]

    def next_or_prev_region(self, direction, regions, point):
        if direction == "next":
            return self.next_region(regions, point)
        else:
            return self.prev_region(regions, point)

    def move_to_section(self, which, where=None):
        if which in range(1, 5):
            sections = self.get_sections()
            if sections and len(sections) >= which:
                section = sections[which - 1]
                self.move_to_region(section)
        elif which in SECTIONS.keys():
            sections = self.get_sections()
            for section in sections:
                if self.section_at_region(section) == which:
                    self.move_to_region(section)
                    return
        elif which in ('next', 'prev'):
            point = self.get_first_point()
            sections = self.get_sections()
            if point and sections:
                next = self.next_or_prev_region(which, sections, point)
                self.move_to_region(next)

    def move_to_item(self, which=1, where=None):
        if which in ('next', 'prev'):
            point = self.get_first_point()
            regions = self.get_status_lines()
            if point and regions:
                next = self.next_or_prev_region(which, regions, point)
                self.move_to_region(next)

    def move_to_file(self, which=1, where=None):
        if isinstance(which, int):
            files = self.get_all_file_regions()
            if files:
                if len(files) >= which:
                    self.move_to_region(self.view.line(files[which - 1]))
                else:
                    self.move_to_region(self.view.line(files[-1]))
            elif self.get_all_stash_regions():
                self.move_to_stash(1)
            elif self.view.find(GIT_WORKING_DIR_CLEAN, 0, sublime.LITERAL):
                region = self.view.find(GIT_WORKING_DIR_CLEAN, 0, sublime.LITERAL)
                self.move_to_region(region)
        elif which in ('next', 'prev'):
            point = self.get_first_point()
            regions = self.get_all_file_regions()
            if point and regions:
                next = self.next_or_prev_region(which, regions, point)
                self.move_to_region(next)
        elif which and where:
            regions = self.get_all_file_regions()
            section_regions = [r for r in regions if self.section_at_region(r) == where]
            if section_regions:
                prev_regions = [r for r in section_regions if self.view.substr(r) < which]
                next_regions = [r for r in section_regions if self.view.substr(r) >= which]
                if next_regions:
                    next = next_regions[0]
                else:
                    next = prev_regions[-1]
                self.move_to_region(next)
            else:
                self.move_to_file(1)

    def move_to_stash(self, which, where=None):
        if which is not None and where:
            which = str(which)
            stash_regions = self.get_all_stash_regions()
            if stash_regions:
                prev_regions = [r for r in stash_regions if self.view.substr(r) < which]
                next_regions = [r for r in stash_regions if self.view.substr(r) >= which]
                if next_regions:
                    next = next_regions[0]
                else:
                    next = prev_regions[-1]
                self.move_to_region(next)
            else:
                self.move_to_file(1)
        elif isinstance(which, int):
            stashes = self.get_all_stash_regions()
            if stashes:
                if len(stashes) >= which:
                    self.move_to_region(self.view.line(stashes[which - 1]))
                else:
                    self.move_to_region(self.view.line(stashes[-1]))


class GitStatusStageCommand(TextCommand, GitStatusTextCmd):

    def run(self, edit, stage="file"):
        goto = None
        if stage == "all":
            self.add_all()
        elif stage == "unstaged":
            self.add_all_unstaged()
        elif stage == "section":
            points = self.get_all_points()
            sections = set([self.section_at_point(p) for p in points])
            if UNTRACKED_FILES in sections and UNSTAGED_CHANGES in sections:
                self.add_all()
            elif UNSTAGED_CHANGES in sections:
                self.add_all_unstaged()
            elif UNTRACKED_FILES in sections:
                self.add_all_untracked()
        elif stage == "file":
            files = self.get_selected_files()
            untracked = [f for s, f in files if s in (UNTRACKED_FILES,)]
            unstaged = [f for s, f in files if s in (UNSTAGED_CHANGES,)]
            if untracked:
                self.add(untracked)
            if unstaged:
                self.add_update(unstaged)
            goto = self.logical_goto_next_file()

        self.update_status(goto)

    def add(self, files):
        return self.git(['add', '--'] + files)

    def add_update(self, files):
        return self.git(['add', '--update', '--'] + files)

    def add_all(self):
        return self.git(['add', '--all'])

    def add_all_unstaged(self):
        return self.git(['add', '--update', '.'])

    def add_all_untracked(self):
        untracked = self.git_lines(['ls-files', '--other', '--exclude-standard'])
        return self.git(['add', '--'] + untracked)


class GitStatusUnstageCommand(TextCommand, GitStatusTextCmd):

    def run(self, edit, unstage="file"):
        goto = None
        if unstage == "all":
            self.unstage_all()
        elif unstage == "file":
            files = self.get_selected_files()
            staged = [f for s, f in files if s == STAGED_CHANGES]
            if staged:
                self.unstage(staged)
                goto = self.logical_goto_next_file()

        self.update_status(goto)

    def no_commits(self):
        return 0 != self.git_exit_code(['rev-list', 'HEAD', '--max-count=1'])

    def unstage(self, files):
        if self.no_commits():
            return self.git(['rm', '--cached', '--'] + files)
        return self.git(['reset', '-q', 'HEAD', '--'] + files)

    def unstage_all(self):
        if self.no_commits():
            return self.git(['rm', '-r', '--cached', '.'])
        return self.git(['reset', '-q', 'HEAD'])


class GitStatusOpenFileCommand(TextCommand, GitStatusTextCmd):

    def run(self, edit):
        repo = find_repo_dir(self.get_cwd())
        files = self.get_selected_files()
        window = self.view.window()

        for s, f in files:
            filename = os.path.join(repo, f)
            window.open_file(filename, sublime.TRANSIENT)


class GitStatusIgnoreCommand(TextCommand, GitStatusTextCmd):

    IGNORE_CONFIRMATION = 'Are you sure you want add the following patterns to .gitignore?'
    IGNORE_LABEL = "Ignore pattern:"

    def run(self, edit, ask=True, edit_pattern=False):
        window = self.view.window()

        files = self.get_selected_files()
        to_ignore = [f for s, f in files if s == UNTRACKED_FILES]

        if not to_ignore:
            return

        if edit_pattern:
            patterns = []
            to_ignore.reverse()

            def on_done(pattern=None):
                if pattern:
                    patterns.append(pattern)
                if to_ignore:
                    filename = to_ignore.pop()
                    window.show_input_panel(self.IGNORE_LABEL, filename, on_done, noop, on_done)
                elif patterns:
                    if ask:
                        if not self.confirm_ignore(patterns):
                            return

                    self.add_to_gitignore(patterns)
                    goto = self.logical_goto_next_file()
                    self.update_status(goto)

            filename = to_ignore.pop()
            window.show_input_panel(self.IGNORE_LABEL, filename, on_done, noop, on_done)
        else:
            if ask:
                if not self.confirm_ignore(to_ignore):
                    return
            self.add_to_gitignore(to_ignore)
            goto = self.logical_goto_next_file()
            self.update_status(goto)

    def confirm_ignore(self, patterns):
        msg = self.IGNORE_CONFIRMATION
        msg += "\n\n"
        msg += "\n".join(patterns[:10])
        if len(patterns) > 10:
            msg += "\n"
            msg += "(%s more...)" % len(patterns) - 10
        return sublime.ok_cancel_dialog(msg, 'Add to .gitignore')

    def add_to_gitignore(self, patterns):
        repo_dir = find_repo_dir(self.get_cwd())
        if repo_dir:
            gitignore = os.path.join(repo_dir, '.gitignore')
            contents = []
            if os.path.exists(gitignore):
                with open(gitignore, 'r+') as f:
                    contents = [l.strip() for l in f]
            logger.debug('Initial .gitignore: %s', contents)
            for p in patterns:
                if p not in contents:
                    logger.debug('Adding to .gitignore: %s', p)
                    contents.append(p)
            with open(gitignore, 'w+') as f:
                f.write("\n".join(contents))
            logger.debug('Final .gitignore: %s', contents)
            return contents


class GitStatusDiscardCommand(TextCommand, GitStatusTextCmd):

    DELETE_UNTRACKED_CONFIRMATION = "Delete all untracked files and directories?"

    def run(self, edit, discard="item"):
        goto = None
        if discard == "section":
            points = self.get_all_points()
            sections = set([self.section_at_point(p) for p in points])
            all_files = self.get_all_files()

            if STASHES in sections:
                if sublime.ok_cancel_dialog('Discard all stashes?', 'Discard'):
                    self.discard_all_stashes()

            if UNTRACKED_FILES in sections:
                if sublime.ok_cancel_dialog(self.DELETE_UNTRACKED_CONFIRMATION, 'Delete'):
                    self.discard_all_untracked()

            if UNSTAGED_CHANGES in sections:
                files = [i for i in all_files if i[0] == UNSTAGED_CHANGES]
                self.discard_files(files)

            if STAGED_CHANGES in sections:
                files = [i for i in all_files if i[0] == STAGED_CHANGES]
                self.discard_files(files)

        elif discard == "item":
            files = self.get_selected_files()
            stashes = self.get_selected_stashes()
            if files:
                self.discard_files(files)
            if stashes:
                self.discard_stashes(stashes)
            goto = self.logical_goto_next_file()

        elif discard == "all":
            self.discard_all()

        self.update_status(goto)

    def discard_all_stashes(self):
        return self.git(['stash', 'clear'])

    def discard_stashes(self, stashes):
        for n, t in stashes:
            if sublime.ok_cancel_dialog('Discard stash %s?' % t):
                self.git(['stash', 'drop', '--quiet', 'stash@{%s}' % n])

    def discard_all_untracked(self):
        return self.git(['clean', '-d', '--force'])

    def is_up_to_date(self, filename):
        return self.git_exit_code(['diff', '--quiet', '--', filename]) == 0

    def get_worktree_status(self, filename):
        output = self.git_string(['diff', '--name-status', '--', filename])
        if output:
            status, _ = output.split('\t')
            return status

    def get_staging_status(self, filename):
        output = self.git_string(['diff', '--name-status', '--cached', '--', filename])
        if output:
            status, _ = output.split('\t')
            return status

    def discard_files(self, files):
        for s, f in files:
            staged = s == STAGED_CHANGES

            if staged and not self.is_up_to_date(f):
                sublime.error_message("Can't discard staged changes to this file. Please unstage it first")
                continue

            if s == UNTRACKED_FILES:
                if sublime.ok_cancel_dialog("Delete %s?" % f, 'Delete'):
                    self.git(['clean', '-d', '--force', '--', f])
            else:
                status = self.get_staging_status(f) if staged else self.get_worktree_status(f)
                if status == 'D':
                    if sublime.ok_cancel_dialog("Resurrect %s?" % f, 'Resurrect'):
                        self.git(['reset', '-q', '--', f])
                        self.git(['checkout', '--', f])
                elif status == 'N':
                    if sublime.ok_cancel_dialog("Delete %s?" % f, 'Delete'):
                        self.git(['rm', '-f', '--', f])
                else:
                    if sublime.ok_cancel_dialog("Discard changes to %s?" % f, 'Discard'):
                        if staged:
                            self.git(['checkout', 'HEAD', '--', f])
                        else:
                            self.git(['checkout', '--', f])

    def discard_all(self):
        if sublime.ok_cancel_dialog("Discard all staged and unstaged changes?", "Discard"):
            if sublime.ok_cancel_dialog("Are you absolutely sure?", "Discard"):
                self.git(['reset', '--hard'])


class GitStatusStashCmd(GitStatusTextCmd, GitStashHelper):

    def pop_or_apply_selected_stashes(self, cmd):
        goto = None
        stashes = self.get_selected_stashes()
        if stashes:
            for name, title in stashes:
                exit_code, stdout = self.git(['stash', cmd, '-q', 'stash@{%s}' % name])
                if exit_code != 0:
                    sublime.error_message(self.format_error_message(stdout))
            if cmd == "apply":
                region = self.view.line(self.get_first_point())
                goto = "point:%s" % region.begin()
            else:
                goto = self.logical_goto_next_stash()

        self.update_status(goto)


class GitStatusStashApplyCommand(TextCommand, GitStatusStashCmd):

    def run(self, edit):
        self.pop_or_apply_selected_stashes('apply')


class GitStatusStashPopCommand(TextCommand, GitStatusStashCmd):

    def run(self, edit):
        self.pop_or_apply_selected_stashes('pop')


class GitStatusDiffCommand(TextCommand, GitStatusTextCmd):

    def run(self, edit):
        files = self.get_selected_files()
        window = self.view.window()

        for s, f in files:
            cached = (s == STAGED_CHANGES)
            window.run_command('git_diff', {'path': f, 'cached': cached})