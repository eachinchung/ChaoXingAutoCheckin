from asyncio import gather, new_event_loop, set_event_loop
from dataclasses import dataclass
from datetime import datetime
from json import dumps, loads
from re import findall
from threading import Lock, Thread
from time import sleep

from redis import ConnectionPool, Redis
from requests import session

active_hash = set([])
pool = ConnectionPool(host='localhost', port=6379)
user_list = [
    {
        'account': {
            'name': '',  # 手机号
            'pwd': ''  # 密码
        },
        'address': {
            'name': '天安门',  # 位置名称
            'longitude': 116.402544,  # 经度
            'latitude': 39.91405  # 纬度
        },
        'SCKEY': None,
        'img_path': 'img/cxk.jpeg',  # 路径为 None 时，直接为cxk照片
    },
    # {
    #     'account': {
    #         'name': '',  # 手机号
    #         'pwd': ''  # 密码
    #     },
    #         'address': {
    #         'name': '天安门',  # 位置名称
    #         'longitude': 116.402544,  # 经度
    #         'latitude': 39.91405  # 纬度
    #     },
    #     'SCKEY': None,
    #     'img_path': None,
    # }
]


@dataclass
class AutoSign:
    user: dict

    uid: int = 1
    name: str = ''

    session = session()
    session.headers = {
        'Accept-Encoding': 'gzip, deflate',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36'
    }

    def login(self):
        """
        登录
        :return:
        """
        response = self.session.post(f'http://passport2.chaoxing.com/api/login', data=self.user['account'])
        response_data = loads(response.text)

        if response_data['result']:
            self.uid = response_data['uid']
            self.name = response_data['realname']
            self.save_caching(response_data)
            print(f"{self.name} 登录成功")
        else:
            raise ValueError(response_data['errorMsg'])

    def save_caching(self, data):
        """
        保存cookies
        """
        caching = {
            'cookies': self.session.cookies.get_dict(),
            'uid': data['uid'],
            'name': data['realname']
        }
        r = Redis(connection_pool=pool)
        r.set(f"checkin-{self.user['account']['name']}", dumps(caching))

    def check_login(self):
        """
        检测登录
        """
        r = Redis(connection_pool=pool)
        caching = r.get(f"checkin-{self.user['account']['name']}")

        if caching is None:
            self.login()

        else:
            caching = loads(caching.decode('utf-8'))

            self.uid = caching['uid']
            self.name = caching['name']

            for item in caching['cookies']:
                self.session.cookies.set(item, caching['cookies'][item])

            # 检测cookies是否有效
            r = self.session.get('http://i.mooc.chaoxing.com/app/myapps.shtml', allow_redirects=False)
            if r.status_code != 200:
                print(f"{self.user['account']['name']} cookies已失效，重新获取中")
                self.login()

    def get_all_class_id(self):
        """
        获取所有课程
        :return:
        """
        response = self.session.get('http://mooc1-1.chaoxing.com/visit/interaction')
        re_rule = r'courseId" value="(.*)" />\s.*classId" value="(.*)" />\s.*\s.*\s.*\s.*\s.*\s.*\s.*\s.*' + \
                  r'\s.*\s*\s.*\s.*\s.*\s.*\s.*title=".*">(.*)</a>'
        return findall(re_rule, response.text)

    async def get_active_id(self, course_id, class_id, course_name):
        """
        访问任务面板获取课程的活动id
        :param course_id:
        :param class_id:
        :param course_name:
        :return:
        """
        re_rule = r'activeDetail\((.*),2.*\s.*\s.*\s.*\s.*green.*\s+\s.*\s+.*\s+.*\s+.*rect">(.*)</a>'
        response = self.session.get('https://mobilelearn.chaoxing.com/widget/pcpick/stu/index', params={
            'courseId': course_id,
            'jclassId': class_id
        })

        res = findall(re_rule, response.text)

        if res:  # 满足签到条件
            if res[0][1][0] == '[':
                checkin_name = f'{course_name} -> {res[0][1][1:-1]}'
            else:
                checkin_name = f'{course_name} -> {res[0][1]}'
            return {
                'class_id': class_id,
                'course_id': course_id,
                'active_id': res[0][0],
                'checkin_name': checkin_name
            }

    def checkin(self, class_id, course_id, active_id):
        """
        普通签到
        :param class_id:
        :param course_id:
        :param active_id:
        :return:
        """
        response = self.session.get('https://mobilelearn.chaoxing.com/widget/sign/pcStuSignController/preSign', params={
            'activeId': active_id,
            'classId': class_id,
            'courseId': course_id
        })
        title = findall('<title>(.*)</title>', response.text)[0]

        if "签到成功" not in title:
            return self.type_recognition(title, class_id, course_id, active_id)
        else:
            sign_date = findall('<em id="st">(.*)</em>', response.text)[0]
            return {
                'date': sign_date,
                'status': title
            }

    def type_recognition(self, checkin_title, class_id, course_id, active_id):
        """
        签到类型识别
        :param checkin_title:
        :param class_id:
        :param course_id:
        :param active_id:
        :return:
        """
        if "手势" in checkin_title:
            return self.gesture_checkin(class_id, course_id, active_id)

        if "位置" in checkin_title:
            return self.location_checkin(active_id)

        if "二维码" in checkin_title:
            return self.qr_code_checkin(active_id)

        return self.photograph_checkin(active_id)

    def gesture_checkin(self, class_id, course_id, active_id):
        """
        手势签到
        :param class_id:
        :param course_id:
        :param active_id:
        :return:
        """
        response = self.session.get('https://mobilelearn.chaoxing.com/widget/sign/pcStuSignController/signIn', params={
            'activeId': active_id,
            'classId': class_id,
            'courseId': course_id
        })
        title = findall(r"<title>(.*)</title>", response.text)[0]
        sign_date = findall(r'<em id="st">(.*)</em>', response.text)[0]

        return {
            'date': sign_date,
            'status': title
        }

    def location_checkin(self, active_id):
        """
        位置签到
        :param active_id:
        :return:
        """
        params = {
            'name': self.name,
            'activeId': active_id,
            'address': self.user['address']['name'],
            'uid': self.uid,
            'latitude': self.user['address']['latitude'],
            'longitude': self.user['address']['longitude'],
            'appType': '15',
            'ifTiJiao': '1'
        }
        response = self.session.get('https://mobilelearn.chaoxing.com/pptSign/stuSignajax', params=params)

        return {
            'date': datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
            'status': response.text
        }

    def qr_code_checkin(self, active_id):
        """
        二维码签到
        :param active_id:
        :return:
        """
        params = {
            'name': self.name,
            'activeId': active_id,
            'uid': self.uid,
            'latitude': -1,
            'longitude': -1,
            'appType': '15'
        }
        response = self.session.get('https://mobilelearn.chaoxing.com/pptSign/stuSignajax', params=params)

        return {
            'date': datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
            'status': response.text
        }

    def photograph_checkin(self, active_id):
        """
        照片签到
        :param active_id:
        :return:
        """
        params = {
            'name': self.name,
            'activeId': active_id,
            'uid': self.uid,
            'latitude': -1,
            'longitude': -1,
            'appType': '15',
            'objectId': self.upload_image()
        }

        response = self.session.get('https://mobilelearn.chaoxing.com/pptSign/stuSignajax', params=params)
        return {
            'date': datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
            'status': response.text
        }

    def upload_image(self):
        """
        上传照片
        :return:
        """
        token = self.session.get('https://pan-yz.chaoxing.com/api/token/uservalid')
        params = {'_token': token.json().get("_token")}
        data = {'puid': self.uid}

        if self.user['img_path'] is None:
            return '8b5f533ee8af0fef4927883af5768e34'

        img = {'file': ('checkin.jpg', open(self.user['img_path'], 'rb'))}
        response = self.session.post('https://pan-yz.chaoxing.com/upload', params=params, data=data, files=img)
        return response.json().get('objectId')

    def auto_run(self):
        """
        自动签到
        :return:
        """

        try:
            tasks = []

            self.check_login()
            course_list = self.get_all_class_id()
            for course in course_list:
                tasks.append(self.get_active_id(course[0], course[1], course[2]))

            loop = new_event_loop()
            set_event_loop(loop)
            result = loop.run_until_complete(gather(*tasks))

            for item in result:
                if item is not None and f"{self.user['account']['name']}-{item['active_id']}" not in active_hash:
                    checking = self.checkin(item['class_id'], item['course_id'], item['active_id'])

                    threadLock.acquire()
                    print(f"{checking['date']} {self.name} {item['checkin_name']} -> {checking['status']}")
                    active_hash.add(f"{self.user['account']['name']}-{item['active_id']}")
                    checkin_log(
                        self.user['account']['name'],
                        f"{checking['date']} {item['active_id']} {item['checkin_name']} -> {checking['status']}\n"
                    )
                    threadLock.release()

                    if self.user["SCKEY"] is not None:
                        self.session.post(f'https://sc.ftqq.com/{self.user["SCKEY"]}.send', data={
                            'text': item['checkin_name'],
                            'desp': f"`{checking['status']}`\n\n######by：{self.name}\n\n###### {checking['date']}"
                        })

            threadLock.acquire()
            print(f"{datetime.today().strftime('%Y-%m-%d %H:%M:%S')} {self.name} 心跳正常")
            threadLock.release()

        except OSError:
            threadLock.acquire()
            print(f"{datetime.today().strftime('%Y-%m-%d %H:%M:%S')} {self.name} 网络连接失败，重试中")
            threadLock.release()


def checkin_log(user_id, checkin_data):
    """
    日志
    :param user_id:
    :param checkin_data:
    :return:
    """
    with open(f"log/{user_id}.log", 'a') as f:
        f.write(checkin_data)


def checkin(account):
    """
    签到线程函数
    :param account:
    :return:
    """
    AutoSign(user=account).auto_run()


def heartbeat():
    """
    心跳检测
    :return:
    """
    while True:
        for user in user_list:
            checkin_thread = Thread(target=checkin, args=[user])
            checkin_thread.start()
            sleep(15)


if __name__ == '__main__':
    threadLock = Lock()
    heartbeat()
