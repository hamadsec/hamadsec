"""
GitHub profile stats generator.

Fetches repo / star / follower / commit / lines-of-code stats via the GitHub
GraphQL API, computes account "uptime" (time since the account was created),
and writes the numbers into dark_mode.svg / light_mode.svg for embedding in the
profile README.

Adapted from Andrew6rant/Andrew6rant. Cleaned up for hamadsec:
  - account-age "uptime" instead of a hardcoded birthdate (no personal data)
  - private repos included in LOC / commit counts
  - Andrew-specific archive handling removed
  - mutable-default-argument bug fixed
  - cache/ auto-created
  - fragile dot-justification replaced with plain in-place value substitution
"""

import datetime
import os
import time
import hashlib

from dateutil import relativedelta
import requests
from lxml import etree

HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0,
               'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def daily_readme(created):
    """
    Returns the length of time since `created` (the account creation date),
    e.g. 'X years, Y months, Z days'. Appends a cake on the account's birthday.
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), created)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years),
        diff.months, 'month' + format_plural(diff.months),
        diff.days, 'day' + format_plural(diff.days),
        ' \U0001F382' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """Returns 's' unless the unit is exactly 1."""
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """Returns a request, or raises an Exception if the response is not 200."""
    request = requests.post('https://api.github.com/graphql',
                            json={'query': query, 'variables': variables},
                            headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code,
                    request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """Returns total commit contributions in a date range."""
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']
               ['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """Returns total repository or star count."""
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if count_type == 'repos':
        return request.json()['data']['user']['repositories']['totalCount']
    elif count_type == 'stars':
        return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment,
                  addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """Fetches 100 commits at a time from a repo, summing my additions/deletions."""
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    # Not using simple_request(): we want to save the cache file before raising.
    request = requests.post('https://api.github.com/graphql',
                            json={'query': query, 'variables': variables},
                            headers=HEADERS)
    if request.status_code == 200:
        if request.json()['data']['repository']['defaultBranchRef'] is not None:
            return loc_counter_one_repo(
                owner, repo_name, data, cache_comment,
                request.json()['data']['repository']['defaultBranchRef']['target']['history'],
                addition_total, deletion_total, my_commits)
        else:
            return 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception("Too many requests in a short amount of time!\n"
                        "You've hit the non-documented anti-abuse limit!")
    raise Exception('recursive_loc() has failed with a', request.status_code,
                    request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history,
                         addition_total, deletion_total, my_commits):
    """Sums LOC for commits authored by me; recurses through commit history pages."""
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    return recursive_loc(owner, repo_name, data, cache_comment,
                         addition_total, deletion_total, my_commits,
                         history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """
    Queries all repositories I have access to (per owner_affiliation), 60 at a
    time, then hands off to cache_builder to compute total lines of code.
    """
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    page = request.json()['data']['user']['repositories']
    if page['pageInfo']['hasNextPage']:
        edges += page['edges']
        return loc_query(owner_affiliation, comment_size, force_cache,
                         page['pageInfo']['endCursor'], edges)
    return cache_builder(edges + page['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    For each repo, re-count LOC only if its commit count changed since last cache.
    Cache keys are sha256(nameWithOwner), so private repo names are not exposed.
    """
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = (repo_hash + ' '
                                   + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount'])
                                   + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n')
            except TypeError:  # empty repo
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """Wipes the cache (keeping the comment block) when the repo set changes."""
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    """Saves partial cache data if the program is about to crash mid-write."""
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,',
          filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """Counts total stars across the given repositories."""
    total_stars = 0
    for node in data:
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def fmt(value):
    """Formats an int with thousands separators; passes strings through."""
    if isinstance(value, int):
        return '{:,}'.format(value)
    return str(value)


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data,
                  contrib_data, follower_data, loc_data):
    """Writes each stat into its matching element id in the SVG."""
    tree = etree.parse(filename)
    root = tree.getroot()
    find_and_replace(root, 'age_data', fmt(age_data))
    find_and_replace(root, 'commit_data', fmt(commit_data))
    find_and_replace(root, 'star_data', fmt(star_data))
    find_and_replace(root, 'repo_data', fmt(repo_data))
    find_and_replace(root, 'contrib_data', fmt(contrib_data))
    find_and_replace(root, 'follower_data', fmt(follower_data))
    find_and_replace(root, 'loc_data', fmt(loc_data[2]))
    find_and_replace(root, 'loc_add', fmt(loc_data[0]))
    find_and_replace(root, 'loc_del', fmt(loc_data[1]))
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def find_and_replace(root, element_id, new_text):
    """Finds the element by id and replaces its text."""
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """Sums my total commits from the cache file built by cache_builder."""
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'r') as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """Returns the account id and creation time of the user."""
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def follower_getter(username):
    """Returns the number of followers of the user."""
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """Counts GitHub GraphQL API calls per function."""
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """Runs a function and returns (result, elapsed_seconds)."""
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference):
    """Prints a formatted timing line."""
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    if difference > 1:
        print('{:>12}'.format('%.4f' % difference + ' s '))
    else:
        print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))


if __name__ == '__main__':
    os.makedirs('cache', exist_ok=True)
    print('Calculation times:')

    # Account id + creation date (used for the "uptime" line).
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)

    created = datetime.datetime.strptime(acc_date, '%Y-%m-%dT%H:%M:%SZ')
    age_data, age_time = perf_counter(daily_readme, created)
    formatter('age calculation', age_time)

    total_loc, loc_time = perf_counter(
        loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)

    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(
        graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data,
                  contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data,
                  contrib_data, follower_data, total_loc[:-1])

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
