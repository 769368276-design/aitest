from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from projects.models import Project
from requirements.models import Requirement
from testcases.models import TestCase
from bugs.models import Bug
import random
from django.utils import timezone

class Command(BaseCommand):
    help = 'Create test data for QA Platform'

    def handle(self, *args, **options):
        User = get_user_model()
        # Ensure we have a user
        user = User.objects.first()
        if not user:
            self.stdout.write(self.style.ERROR('No users found. Please run init_data or create a user first.'))
            return

        self.stdout.write('Creating test data...')

        # 1. Create Projects
        projects = []
        for i in range(1, 6):
            project = Project.objects.create(
                name=f'测试项目 {i} - 电商系统',
                description=f'这是一个用于测试的电商平台项目 {i}，包含订单、用户、支付等模块。',
                owner=user,
                status=random.choice([1, 2, 3]),
                start_time=timezone.now(),
            )
            projects.append(project)
            self.stdout.write(f'Created Project: {project.name}')

        # 2. Create Requirements
        req_types = [1, 2, 3] # Functional, Non-functional, Optimization
        req_priorities = [1, 2, 3] # High, Medium, Low
        
        for project in projects:
            for j in range(1, 6):
                req = Requirement.objects.create(
                    project=project,
                    title=f'需求 {j}: {project.name} 的核心功能',
                    description='作为用户，我希望能够使用手机号快速登录，以便于...',
                    type=random.choice(req_types),
                    priority=random.choice(req_priorities),
                    status=random.choice([1, 2, 3, 4]),
                    creator=user,
                )
                self.stdout.write(f'  Created Requirement: {req.title}')

                # 3. Create Test Cases for each Requirement
                for k in range(1, 4):
                    case = TestCase.objects.create(
                        project=project,
                        requirement=req,
                        title=f'用例 {k}: 验证 {req.title}',
                        pre_condition='用户已进入登录页面',
                        steps='1. 输入手机号\n2. 输入验证码\n3. 点击登录',
                        expected_result='登录成功，跳转至首页',
                        type=random.choice([1, 2, 3]),
                        priority=random.choice([1, 2, 3]),
                        status=random.choice([0, 1, 2, 3]),
                        creator=user,
                    )
                    
                    # 4. Create Bugs (randomly)
                    if random.choice([True, False]):
                        bug = Bug.objects.create(
                            project=project,
                            case=case,
                            title=f'Bug: {case.title} 执行失败',
                            description='点击登录按钮无反应，控制台报错 500',
                            reproduce_steps=case.steps,
                            severity=random.choice([1, 2, 3, 4]),
                            priority=random.choice([1, 2, 3]),
                            status=random.choice([1, 2, 3]),
                            creator=user,
                            assignee=user,
                            affected_version='v1.0.0'
                        )
                        self.stdout.write(f'    Created Bug: {bug.title}')

        self.stdout.write(self.style.SUCCESS('Test data created successfully!'))
