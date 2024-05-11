import datetime
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from json import JSONDecodeError

import requests
from apscheduler.events import EVENT_JOB_ERROR, EVENT_ALL, EVENT_JOB_EXECUTED
from apscheduler.schedulers.blocking import BlockingScheduler
from bs4 import BeautifulSoup

from util.common import get_config, parse_execution_time, exit_if_necessary, load_data, clear, \
    kill_process, get_separator
from util.email_util import send_email
from util.user_agent_util import get_random_ua

err_message = '请检查网络（代理、梯子等）是否正确.'
success_message = '---> 成功.'
success_message2 = '---> 成功，'
fail_message = '---> 失败.'
fail_message2 = '---> 失败，'
UA = None
logger = logging.getLogger("Automated Redemption")
logger.setLevel(logging.INFO)
separator = get_separator()
COOKIE = {'Cookie': '***'}
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_html_for_data(html):
    soup = BeautifulSoup(html, 'html.parser')
    rewards_calendar_ele = soup.find('section', {'class': 'js-rewards-calendar'})

    meta_ele = soup.find('meta', {'name': 'csrf-token'})
    # 表示是否已经全部签到完成（无可再签）
    calendar_id = rewards_calendar_ele.attrs['data-calendar-id'] if rewards_calendar_ele is not None else None
    # current_reward = soup.find('div', {'class': 'reward-status-current-not-claimed'})
    future_reward = soup.find('div', {'class': 'reward-status-future'})
    return {
        'csrf_token': meta_ele.attrs['content'],
        'calendar_id': calendar_id,
        'destination': calendar_id is not None and future_reward is None and soup.find('div', {'class': 'reward-status-current-not-claimed'}) is None
    }


# 获取网站主页
def get_nutaku_home(cookies, proxies, config):
    url = "https://www.nutaku.net/home/"
    cookies['isIpad'] = 'false'
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "User-Agent": UA,
        "Cookie": urllib.parse.urlencode(cookies).replace("&", ";") if cookies is not None else "",
        'Host': 'www.nutaku.net',
        'Origin': 'https://www.nutaku.net',
        'Referer': 'https://www.nutaku.net/home/'
    }
    logger.debug("headers->{}".format(headers | COOKIE))
    timeout = config.get('settings', 'connection_timeout')
    resp = requests.get(url, headers=headers, proxies=proxies, timeout=int(timeout))
    if resp.status_code == 200:
        return resp
    raise RuntimeError(fail_message2 + err_message)


# 签到获取金币
def get_rewards(cookies, html_data, proxies, config):
    url = 'https://www.nutaku.net/rewards-calendar/redeem/'
    Cookie = "NUTAKUID={}; Nutaku_TOKEN={}; isIpad=false"
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-CSRF-TOKEN": html_data.get("csrf_token"),
        "User-Agent": UA,
        "Cookie": Cookie,
        'Origin': 'https://www.nutaku.net',
        'Referer': 'https://www.nutaku.net/home/',
        "X-Requested-With": "XMLHttpRequest",
        'Host': 'www.nutaku.net'
    }

    data = "calendarId={}".format(html_data.get('calendar_id'))

    logger.debug("data->{}".format(data))
    logger.debug("headers->{}".format(headers))
    timeout = config.get('settings', 'connection_timeout')
    headers['Cookie'] = Cookie.format(cookies.get("NUTAKUID"), cookies.get("Nutaku_TOKEN"))
    resp = requests.post(url, headers=headers, data=data, proxies=proxies, timeout=int(timeout))
    # 请求成功时，将会返回{"userGold": "1"}
    logger.debug("status_code->{}".format(resp.status_code))
    logger.debug("resp_text->{}".format(resp.text))
    status_code = resp.status_code
    # if status_code == 422:
    #     resp_data = resp.json()
    #     msg = resp_data.get('message')
    #     # 未知原因，需要重试
    #     if msg == "Couldn't identify reward":
    #         raise RuntimeError(fail_message2 + msg)
    #     resp_data['code'] = status_code
    #     return resp_data
    # 20240511更新：如果状态码不为200，则当做错误处理，以覆盖首次签到失败后（概率失败），后续不再进行签到的问题；
    # 会有一种很极端的情况，如用户当天手动签到后，程序会一直报错，直至最大重试次数；（没办法，服务器又不返回特定的状态码）
    if status_code == 200:
        try:
            return resp.json()
        except JSONDecodeError:
            raise RuntimeError(fail_message2 + err_message)
    raise RuntimeError(fail_message2 + err_message)


# 签到获取的物件，除了金币以外，还有优惠卷
def reward_resp_data_handler(resp_data: dict, data: dict):
    item = resp_data.get('userGold')
    _content = "当前签到物件为未知物件"
    if item is not None:
        print("---> 当前金币为：" + item + "\n")
        _content = f'当前账号金币为：{item}'
        data['user_gold'] = item
    elif resp_data.get('coupon') is not None:
        item = resp_data.get('coupon')
        _content = "获取到优惠卷：{}/{}".format(item.get('title'), item.get('code'))
        # print("---> 当前签到物件为优惠卷：" + item + "\n")
        print("---> " + _content + "\n")
    else:
        print("---> " + _content + "\n")
    data['content'] = _content
    # 邮箱通知
    if config.get('settings', 'email_notification') == 'on':
        send_email(config=config, data=data, logger=logger)


def getting_rewards_handler(cookies, proxies, config, html_data, local_data):
    print('---> 开始签到.')
    reward_resp_data = get_rewards(cookies=cookies, html_data=html_data, proxies=proxies, config=config)
    logger.debug("resp_data->{}".format(reward_resp_data))
    status_code = reward_resp_data.get('code')

    data_file_path = config.get('sys', 'dir') + separator + 'data.json'
    data = {
        'date': datetime.datetime.now().strftime('%Y-%m-%d'),
        'email': config.get('account', 'email'),
        'utc_date': datetime.datetime.utcnow().strftime('%Y-%m-%d'),
        'limit_str': local_data.get('limit').strftime(DATE_FORMAT)
    }
    if status_code is not None and status_code == 422:
        logger.debug("结果->重复签到或其他（多为前者）.")
        print('---> {} 已经签到.'.format(data.get('date')))
    else:
        print(success_message)
        reward_resp_data_handler(reward_resp_data, data)

    # 创建文件
    if os.path.exists(data_file_path) is False:
        with open(data_file_path, 'w'):
            pass

    with open(data_file_path, 'r+') as _file:
        json_str = _file.read()
        is_not_empty = len(json_str) > 0
        merged = (json.loads(json_str) if is_not_empty else {}) | data
        # 清空文件内容，再重新写入
        if is_not_empty:
            _file.seek(0)
            _file.truncate()
        json.dump(merged, _file)


# 登陆nutaku账号；
# 请求成功后，将返回的cookie存储与本地文件中，以便后续使用；
def login(config, cookies, proxies, csrf_token):
    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh-Hans;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": UA,
        "X-Csrf-Token": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": "NUTAKUID={}; isIpad=false;".format(cookies['NUTAKUID']),
        'Host': 'www.nutaku.net',
        'Origin': 'https://www.nutaku.net',
        'Referer': 'https://www.nutaku.net/home/'
    }

    data = "email={}&password={}&rememberMe=1&pre_register_title_id="
    logger.debug('headers->{}'.format(headers))
    logger.debug('data->{}'.format(data))
    url = 'https://www.nutaku.net/execute-login/'
    timeout = config.get('settings', 'connection_timeout')
    resp = requests.post(url, headers=headers,
                         data=data.format(config.get('account', 'email'), config.get('account', 'password')),
                         proxies=proxies, timeout=int(timeout))
    # 返回的是一个重定向链接，token是在cookie中
    # {"redirectURL":"https:\/\/www.nutaku.net\/home"}
    if resp.status_code == 200:
        return resp
    return RuntimeError(fail_message2 + err_message)


def logging_in_handler(config, cookies, cookie_file_path, proxies, html_data, local_data):
    login_resp = login(config=config, cookies=cookies, proxies=proxies, csrf_token=html_data.get("csrf_token"))
    try:
        resp_data = login_resp.json()
        logger.debug("resp_data->{}".format(resp_data))
        if resp_data['redirectURL'] is not None:
            login_cookies = login_resp.cookies.get_dict()
            with open(cookie_file_path, 'w') as _file:
                json.dump(login_cookies, _file)
            print(success_message)
            print('---> 重新请求nutaku主页，并获取calendar_id.')
            cookies = cookies | login_cookies
            home_resp = get_nutaku_home(cookies=cookies, proxies=proxies, config=config)
            cookies = cookies | home_resp.cookies.get_dict()
            html_data = parse_html_for_data(home_resp.text)
            logger.debug("html_data->{}".format(html_data))
            if html_data.get("destination"):
                print("恭喜，已经全部签到完成.")
                kill_process()
                return
            if html_data.get("calendar_id") is not None:
                print(success_message)
                getting_rewards_handler(cookies=cookies, html_data=html_data, proxies=proxies, config=config,
                                        local_data=local_data)
            else:
                raise RuntimeError(fail_message2 + err_message)
        elif resp_data['status'] == 'error':
            logger.debug("签到出现异常.")
            print('---> 账号或密码错误，请重新输入后再启动程序.')
            kill_process()
    except JSONDecodeError:
        logger.debug("登陆失败，未知原因.")
        raise RuntimeError(fail_message2 + err_message)


def redeem(config, clearing=False, local_data: dict = None, reloading=False):
    # 重新加载数据
    if reloading:
        local_data = load_data(config, logger)
    set_limit_time(local_data)
    if clearing:
        clear(True)
    if not check(True, local_data):
        global UA
        UA = get_random_ua()
        cookie_file_path = config.get('sys', 'dir') + separator + 'cookies.json'
        # 尝试读取本地cookie文件
        local_cookies = {}
        print('---> 读取本地cookie.')
        if os.path.exists(cookie_file_path):
            with open(cookie_file_path, 'r') as file:
                json_str = file.read()
                if len(json_str) > 0:
                    _local_cookies = json.loads(json_str)
                    _email = local_data.get('email')
                    logger.debug("记录的账号->{}".format(_email))
                    if _email is not None:
                        if _email == config.get('account', 'email'):
                            local_cookies = _local_cookies
                            print(success_message)
                        else:
                            print('---> 检测到账号发生变化，停止使用当前加载的cookie.')
                    else:
                        print('---> 记录的账号为空，停止使用当前加载的cookie.')
                else:
                    print('---> 文件内容为空.')
        else:
            print('---> 本地cookie不存在.')

        proxies = {}
        if config.get('network', 'proxy') == 'on':
            proxies['http'] = config.get('network', 'http')
            proxies['https'] = config.get('network', 'https')
            logger.debug("启用代理->{}".format(proxies))
        print('---> 请求nutaku主页.')
        home_resp = get_nutaku_home(cookies=local_cookies, proxies=proxies, config=config)
        # 合并cookie，以使用新的XSRF-TOKEN、NUTAKUID
        merged = local_cookies | home_resp.cookies.get_dict()
        print(success_message)
        print('---> 获取calendar_id与csrf_token.')
        html_data = parse_html_for_data(home_resp.text)
        logger.debug("html_data->{}".format(html_data))
        if html_data.get("destination"):
            print("恭喜，已经全部签到完成.")
            kill_process()
            return
        # 未登陆或登陆已失效
        if html_data.get('calendar_id') is None:
            print(fail_message2 + '未登陆或登陆过期')
            if local_cookies.get('Nutaku_TOKEN') is not None:
                print('---> 尝试重新登陆账号.')
            else:
                print('---> 登陆账号.')
            # 登陆返回的cookie包含Nutaku_TOKEN
            logging_in_handler(config=config, cookies=merged, cookie_file_path=cookie_file_path,
                               proxies=proxies, html_data=html_data, local_data=local_data)
        else:
            print(success_message)
            getting_rewards_handler(cookies=merged, html_data=html_data, proxies=proxies, config=config,
                                    local_data=local_data)


def listener(event, sd, conf):
    if event.code == EVENT_JOB_EXECUTED:
        logger.debug("任务执行完成.")
        if event.job_id == '001' or event.job_id == '002':
            exit_if_necessary(conf, logger)
    elif event.code == EVENT_JOB_ERROR:
        today = datetime.datetime.today()
        local_data = load_data(conf, logger)
        limit_str = local_data.get('limit_str')
        limit = None
        # 限制时间是第二天的早上8点，因此，一般情况下limit和today不在同一天；而如果处于同一天时，说明limit还没更新（比如没有请求n站成功就进入了重试逻辑）
        if limit_str is not None:
            limit = datetime.datetime.strptime(limit_str, DATE_FORMAT)
        # 获取当前时间，加上时间间隔
        next_time = get_next_time(int(conf.get('settings', 'retrying_interval')))
        logger.debug("当前时间：{}".format(today))
        logger.debug("截止日期：{}".format(limit))
        matched = today.day == limit.day
        if matched:
            logger.debug("截止日期未更新")
        _retrying = int(conf.get('settings', '_retrying'))
        if _retrying > 1:
            _retrying -= 1
            # conf.set('settings', '_retrying', _retrying)
            setRetryingCopying(conf, str(_retrying))
            if limit is None or next_time < limit or matched:
                print(f'---> 将会在{next_time}进行重试.')
                # 如果是001时，删除002任务，以免出现冲突，即如果id=001的任务出现错误时，还在等待中的id=002的任务将会被清除
                if event.job_id == '001':
                    job = sd.get_job('002')
                    if job is not None:
                        sd.remove_job('002')
                sd.add_job(id='002', func=redeem, trigger='date', next_run_time=next_time,
                           args=[conf, False, local_data],
                           misfire_grace_time=config.getint('settings', 'misfire_grace_time') * 60)
            else:
                dateFormat = '{}-{}-{}'.format(today.year, today.month, today.day)
                print('---> {} 签到失败，已到达最大重试次数.'.format(dateFormat))
                setRetryingCopying(conf, conf.get('settings', 'retrying'))
                exit_if_necessary(conf, logger)
        else:
            print('---> 已到达最大重试次数，将停止签到；如本日签到还未完成时，请手动签到.')
            exit_if_necessary(conf, logger)


def get_next_time(minutes):
    next_time = datetime.datetime.now() + datetime.timedelta(minutes=minutes)
    return next_time


# 设置签到截止日期
def set_limit_time(local_data: dict):
    today = datetime.datetime.today()
    # 获取第二天早上8点0分0秒的时间，即utc+0的00:00:00，若当前时间已经超过该时间点，将不再执行
    # limit是相对于today_000的时间
    today_000 = today.replace(hour=0, minute=0, second=0)
    limit = today_000 + datetime.timedelta(days=1, hours=8)
    _limit = local_data.get("limit")
    if _limit != limit:
        local_data['limit'] = limit


def wrapper(fn, sd, conf):
    def inner(event):
        return fn(event, sd, conf)

    return inner


# 检查任务是否已经执行；True表示已经签到，False表示未签到
def check(printing: bool = True, local_data: dict = None):
    now = datetime.datetime.utcnow()
    current_utc = now.strftime('%Y-%m-%d')
    print('---> 检查中...')
    utc_date = local_data.get('utc_date')
    if utc_date is None or utc_date != current_utc:
        if printing:
            print('---> 即将执行签到.')
        return False
    if printing:
        print('---> {} 签到已完成.'.format(local_data.get('date')))
    return True


def get_dict_params(mode, execution_time):
    params = {}
    if mode == '1':
        params['hour'] = execution_time['hours']
        params['minute'] = execution_time['minutes']
        params['trigger'] = 'cron'
    else:
        params['trigger'] = 'date'
        params['next_run_time'] = get_next_time(1)
    return params


# 使用额外线程，每30分钟唤醒一次scheduler
def jobs_checker(sc):
    while True:
        logger.debug('->{} 任务检查线程休眠...'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        time.sleep(60 * 39)
        logger.debug(
            '->{} 任务检查线程休眠；唤醒定时任务调度器...'.format(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        sc.wakeup()


def setRetryingCopying(conf, value):
    conf.set('settings', '_retrying', value)


"""
todo：1、Nutaku_ageGateCheck是秒数，如果到期了，那估计还需要调用对应的接口（are you over 18 years old？）；
"""
if __name__ == '__main__':
    clear(True)
    current_dir = os.path.dirname(sys.argv[0])
    print('---> 当前目录为：' + current_dir)
    print('---> 读取配置文件.')
    config = get_config(current_dir, logger)
    config.add_section('sys')
    config.set('sys', 'dir', current_dir)
    setRetryingCopying(config, config.get('settings', 'retrying'))
    print(success_message)
    mode = config.get('settings', 'execution_mode')
    if config.get('settings', 'debug') == 'on':
        logging.basicConfig()
        logging.getLogger('apscheduler').setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    scheduler = BlockingScheduler()
    execution_time = parse_execution_time(config.get('settings', 'execution_time'))

    scheduler.add_listener(wrapper(listener, scheduler, config), EVENT_ALL)
    scheduler.add_job(id='001', func=redeem, **get_dict_params(mode, execution_time),
                      args=[config, True, None, True],
                      misfire_grace_time=config.getint('settings', 'misfire_grace_time') * 60)

    try:
        if mode == '1':
            jobs_checker_thread = threading.Thread(target=jobs_checker, args=(scheduler,))
            jobs_checker_thread.start()
        scheduler.start()
    except BaseException as e:
        logger.debug(f"捕获异常->{e}")
        print('---> 退出程序.')
