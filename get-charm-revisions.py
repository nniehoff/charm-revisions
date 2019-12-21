#!/usr/bin/env python3
"""
This script / module is created to work with charmreleases.
It will build a yaml file, containing for every charmers release an charmstore
revision and the charmers release where it belongs to.

It does this by:
    1. Scraping the charmstore
    2. Match sha commits against stable branches on github
    3. repeat for every listed module.

"""

import collections
import os
import re
from contextlib import ExitStack
from functools import lru_cache

import macaroonbakery
import theblues
import yaml
from github import Github
from theblues.charmstore import CharmStore

CHARMSTORE_API = "https://api.jujucharms.com/charmstore/v5"
STABLE_COMMIT_LOOKBACK = 20

file_manger = ExitStack()
CHARM_YAML = "charm_revisions.yaml"

class CharmInfo:
    def __init__(self, charm_name, last_checked_revision=0, debug=False):
        self.name = charm_name
        self.last_checked_revision = last_checked_revision
        self.cs = CharmStore(CHARMSTORE_API)
        self._debug = debug

    @property
    def get_charmstore_revisions(self):
        """Build a list of revisions as found on the charmstore."""

        self.debug(f"Searching for entity [{self.name}]")
        entity = self.cs.entity(self.name)
        match = re.search(r"-(\d+)$", entity["Id"])
        last_revision = int(match.group(1))
        self.debug(f"Highest revision number found [{last_revision}]")

        revision_hash = {}

        if last_revision == 0:
            # When dealing with the first release we have to trick it a bit
            self.last_checked_revision = -1
        for revision in range(last_revision, self.last_checked_revision, -1):
            self.debug(f"Attempt to get sha for revision [{revision}]")
            cs_location = f"{self.name}-{revision}"
            retry = 3
            cs_files = None

            # Retry if we don't have metadata & our retry counter is positive
            while retry > 0 and cs_files is None:
                try:
                    cs_files = self.cs.files(cs_location)
                    self.debug("Found an entry in the charmstore")
                    # If we have metadata we can reset the retry, if an
                    # exception is raised, we won't reach this point
                    retry = 3
                except theblues.errors.EntityNotFound:
                    self.debug(f"Missing charmstore files for [{cs_location}]")
                    # No need to retry the file does not exist
                    retry = -1
                except macaroonbakery.httpbakery._error.InteractionError:
                    self.debug("Login issue, skipping")
                    retry = -1
                except theblues.errors.ServerError:
                    self.debug(f"Timeout, retries left {retry}")
                    # If we have a time out we decrease our counter
                    retry = retry - 1
                else:
                    # we got files, so we can check if we can have the
                    # repo-info file
                    if not "repo-info" in cs_files:
                        # repo-info is introduced around version 17.0x
                        self.debug("Missing repo-info entry")
                        continue

                    # If we are here, we know we can expect an repo-info file
                    repo_info = None
                    while not repo_info:
                        try:
                            repo_info = self.cs.files(
                                cs_location, filename="repo-info", read_file=True
                            )
                        except theblues.errors.ServerError:
                            self.debug("Retry getting repo-info")
                        else:
                            sha_match = re.search(
                                r"commit-sha-1: ([\da-f]+)", repo_info
                            )
                            if sha_match is not None:
                                revision_hash[revision] = {"sha": sha_match.group(1)}
                                repo_match = re.search(
                                    r"remote: https://github.com/(.+)/(.+)", repo_info
                                )
                                if repo_match is not None:
                                    revision_hash[revision]["user"] = repo_match.group(
                                        1
                                    )
                                    revision_hash[revision]["repo"] = repo_match.group(
                                        2
                                    )
                            else:
                                self.debug("Could not locate a hash, skipping")
                                continue
        return revision_hash

    def debug(self, log):
        if self._debug:
            print(log)


class CharmGit:
    def __init__(self, user, repo, debug=False):
        self.user = user
        self.repo = repo
        self._debug = debug
        if "GITHUB_USER" in os.environ and "GITHUB_TOKEN" in os.environ:
            self.git = Github(os.environ["GITHUB_USER"], os.environ["GITHUB_TOKEN"])
        else:
            self.git = Github()

    @property
    def stable_sha_dict(self):
        commits = collections.OrderedDict()
        repo = self.git.get_user(self.user).get_repo(self.repo)
        for branch in repo.get_branches():
            is_stable = re.search(r"^stable/.+$", branch.name)
            if not is_stable:
                continue

            self.debug(f"Found stable branch {branch.name}")

            limit = STABLE_COMMIT_LOOKBACK
            for num, commit in enumerate(repo.get_commits(sha=branch.name), 1):
                commits[commit.sha] = branch.name
                if num == limit:
                    break
        return commits

    def debug(self, log):
        if self._debug:
            print(log)


def main():
    with open(CHARM_YAML) as f:
        charm_data = yaml.load(f, Loader=yaml.FullLoader)

    # Make a copy as we will modify the data during operation
    charmlist = sorted(list(charm_data.keys()))
    for charm in charmlist:
        details = charm_data[charm]

        # In this case we are dealing with an new entry in the yaml file,
        # so let's make sure we create a dict out of it
        if not isinstance(details, dict):
            details = {}
            charm_data[charm] = details
        try:
            last_revision = details.get("last_revision", 0)
        except AttributeError:
            last_revision = 0
        ci = CharmInfo(charm, debug=True, last_checked_revision=last_revision)
        revision_detail = ci.get_charmstore_revisions

        # If we have no new revision info, we can skip the rest of the
        # processing
        if len(revision_detail) == 0:
            continue

        # We currently only support github based repo's and we assume that the
        # repo name has not changed. So get all the release hashes with a
        # single call
        hashes = None
        for revision in revision_detail:
            print(f"Searching for gitbub info for revision [{revision}]")
            revision_data = revision_detail[revision]
            if "user" in revision_data:
                git_info = CharmGit(
                    user=revision_data["user"], repo=revision_data["repo"], debug=True
                )
                hashes = git_info.stable_sha_dict
                break
            else:
                print("Could not locate user in: {}".format(revision_detail[revision]))

        if hashes is not None:
            for revision in revision_detail:
                revision_data = revision_detail[revision]
                for commit_sha, branch in hashes.items():
                    if revision_data["sha"] == commit_sha:
                        revision_data["release"] = branch
                        break
                print(f"Updating details for revision [{revision}]")
                try:
                    charm_data[charm][revision].update(revision_data)
                except KeyError:
                    charm_data[charm][revision] = revision_data

            with open(CHARM_YAML, "w") as f:
                yaml.dump(charm_data, f, sort_keys=True)

        # Make sure we record the entries we have handled, and store it
        charm_data[charm]["last_revision"] = sorted(revision_detail.keys())[-1]
        with open(CHARM_YAML, "w") as f:
            yaml.dump(charm_data, f, sort_keys=True)


if __name__ == "__main__":
    main()