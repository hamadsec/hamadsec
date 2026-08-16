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
               'recursive_loc': 0, 'contributions_getter': 0, 'loc_query': 0,
               'language_getter': 0}

# Repos the API listed but would not hand over (see skip_hidden). Counted so a
# permissions regression shows up as a warning instead of silently shrinking
# the numbers.
HIDDEN_REPOS = 0


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


def skip_hidden(edges):
    """
    Filters out repository edges GitHub returned as null.

    A repo the token cannot read still occupies an edge in the connection (and
    still counts toward totalCount) but arrives as {"node": null}. That is what
    a private repo looks like once ACCESS_TOKEN loses `repo` scope. Dropping the
    edge keeps the run alive; HIDDEN_REPOS makes the loss visible afterwards.
    """
    global HIDDEN_REPOS
    kept = []
    for edge in edges:
        if edge is None or edge.get('node') is None:
            HIDDEN_REPOS += 1
        else:
            kept.append(edge)
    return kept


def iso(moment):
    """Formats a naive UTC datetime the way the GraphQL DateTime scalar wants."""
    return moment.strftime('%Y-%m-%dT%H:%M:%SZ')


def contributions_getter(start_date, end_date):
    """
    Returns the contributionsCollection for a window of at most one year:
    commit / PR / issue / review counts plus the day-by-day calendar.

    `restrictedContributionsCount` is the private-repo share, which is only
    populated when the token may read those repos — so it moves in lockstep
    with the null-node problem skip_hidden() reports on.
    """
    query_count('contributions_getter')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                totalCommitContributions
                restrictedContributionsCount
                totalPullRequestContributions
                totalIssueContributions
                totalPullRequestReviewContributions
                contributionCalendar {
                    totalContributions
                    weeks {
                        contributionDays {
                            date
                            contributionCount
                        }
                    }
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(contributions_getter.__name__, query, variables)
    return request.json()['data']['user']['contributionsCollection']


def contribution_history(created):
    """
    Walks the account year by year (the collection window caps at 12 months) and
    returns (days, this_year), where `days` maps 'YYYY-MM-DD' -> contribution
    count over the account's whole life and `this_year` is the current calendar
    year's collection.
    """
    days = {}
    this_year = None
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    for year in range(created.year, now.year + 1):
        start = max(created, datetime.datetime(year, 1, 1))
        end = min(now, datetime.datetime(year, 12, 31, 23, 59, 59))
        if start >= end:
            continue
        collection = contributions_getter(iso(start), iso(end))
        for week in collection['contributionCalendar']['weeks']:
            for day in week['contributionDays']:
                days[day['date']] = max(days.get(day['date'], 0),
                                        day['contributionCount'])
        if year == now.year:
            this_year = collection
    return days, this_year


def streak_counter(days):
    """
    Returns (current, longest) contribution streaks in days.

    An empty *today* does not break the current streak — the day is still in
    progress, and counting it as a break would make the number flicker to 0
    every midnight UTC.
    """
    active = sorted(date for date, count in days.items() if count > 0)
    if not active:
        return 0, 0

    longest = run = 1
    for index in range(1, len(active)):
        gap = (datetime.date.fromisoformat(active[index])
               - datetime.date.fromisoformat(active[index - 1])).days
        run = run + 1 if gap == 1 else 1
        longest = max(longest, run)

    day = datetime.date.today()
    if days.get(day.isoformat(), 0) == 0:
        day -= datetime.timedelta(days=1)
    current = 0
    while days.get(day.isoformat(), 0) > 0:
        current += 1
        day -= datetime.timedelta(days=1)
    return current, longest


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
    # Same null-node hazard as stars_counter, and worse here: cache_builder
    # indexes the cache file positionally against these edges.
    page_edges = skip_hidden(page['edges'])
    if page['pageInfo']['hasNextPage']:
        edges += page_edges
        return loc_query(owner_affiliation, comment_size, force_cache,
                         page['pageInfo']['endCursor'], edges)
    return cache_builder(edges + page_edges, comment_size, force_cache)


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
    for node in skip_hidden(data):
        total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def fmt(value):
    """Formats an int with thousands separators; passes strings through."""
    if isinstance(value, int):
        return '{:,}'.format(value)
    return str(value)


def svg_overwrite(filename, stats):
    """Writes each stat into the SVG element carrying the matching id."""
    tree = etree.parse(filename)
    root = tree.getroot()
    for element_id, value in stats.items():
        find_and_replace(root, element_id, fmt(value))
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def language_getter(top_n=3, cursor=None, totals=None):
    """
    Aggregates bytes-per-language across owned (incl. private) repos and returns
    a compact string of the top languages by share, e.g.
    'Python 82% - HTML 9% - CSS 5%'.

    top_n is 3 because the card's value column fits ~44 monospace characters
    before it runs into the right edge.

    Archived repos are excluded. With them in, a 2019 Java coursework project
    outweighed everything current and the card read 'Java 54%'.
    """
    if totals is None:
        totals = {}
    query_count('language_getter')
    query = '''
    query ($login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER], isFork: false, isArchived: false) {
                nodes {
                    languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
                        edges { size node { name } }
                    }
                }
                pageInfo { hasNextPage endCursor }
            }
        }
    }'''
    variables = {'login': USER_NAME, 'cursor': cursor}
    request = simple_request(language_getter.__name__, query, variables)
    repos = request.json()['data']['user']['repositories']
    for node in repos['nodes']:
        if node is None:  # unreadable repo — see skip_hidden()
            continue
        for edge in node['languages']['edges']:
            name = edge['node']['name']
            totals[name] = totals.get(name, 0) + edge['size']
    if repos['pageInfo']['hasNextPage']:
        return language_getter(top_n, repos['pageInfo']['endCursor'], totals)

    grand = sum(totals.values())
    if grand == 0:
        return 'n/a'
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return ' - '.join(f'{name} {round(size / grand * 100)}%' for name, size in top)


def find_and_replace(root, element_id, new_text):
    """
    Finds the element by id and replaces its text.

    A missing id used to pass silently, which is how `lang_data` was computed on
    every run and then thrown away for want of a slot in the SVG. Say so instead.
    """
    element = root.find(f".//*[@id='{element_id}']")
    if element is None:
        print(f'::warning::no SVG element with id={element_id!r} — '
              f'the value {new_text!r} was computed and discarded')
        return
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

    # language_getter() is deliberately NOT called: the card's Languages.Code
    # line is hand-written, because the computed mix is dominated by whichever
    # side project is largest rather than by what the work actually is. The
    # function stays for whenever that line should go live.

    (days, this_year), hist_time = perf_counter(contribution_history, created)
    formatter('contributions', hist_time)
    current_streak, longest_streak = streak_counter(days)

    # Commits made this calendar year, private repos included. The restricted
    # count is only non-zero while the token may read them.
    commits_year = (this_year['totalCommitContributions']
                    + this_year['restrictedContributionsCount']) if this_year else 0

    year_days = {date: count for date, count in days.items()
                 if date.startswith(str(datetime.date.today().year))}
    active_days = sum(1 for count in year_days.values() if count > 0)
    best_day = max(year_days.values()) if year_days else 0

    stats = {
        'age_data': age_data,
        'commit_data': commit_data,
        'commit_year': commits_year,
        'star_data': star_data,
        'repo_data': repo_data,
        'contrib_data': contrib_data,
        'follower_data': follower_data,
        'loc_data': total_loc[2],
        'loc_add': total_loc[0],
        'loc_del': total_loc[1],
        'active_days': active_days,
        'best_day': best_day,
        'streak_data': f'{current_streak} day{format_plural(current_streak)}',
        'streak_best': f'{longest_streak} day{format_plural(longest_streak)}',
    }
    svg_overwrite('dark_mode.svg', stats)
    svg_overwrite('light_mode.svg', stats)

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))

    if HIDDEN_REPOS:
        print(f'::warning::{HIDDEN_REPOS} repository node(s) came back null, so '
              'their stars, commits and lines of code are missing from this card. '
              'That is what a private repo looks like when ACCESS_TOKEN has lost '
              '`repo` scope — check the token.')
