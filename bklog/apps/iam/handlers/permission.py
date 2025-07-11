"""
Tencent is pleased to support the open source community by making BK-LOG 蓝鲸日志平台 available.
Copyright (C) 2021 THL A29 Limited, a Tencent company.  All rights reserved.
BK-LOG 蓝鲸日志平台 is licensed under the MIT License.
License for BK-LOG 蓝鲸日志平台:
--------------------------------------------------------------------
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all copies or substantial
portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
We undertake not to change the open source license (MIT license) applicable to the current version of
the project delivered to anyone in the future.
"""

from django.conf import settings
from django.utils.translation import gettext as _
from iam import (
    MultiActionRequest,
    ObjectSet,
    Request,
    Resource,
    Subject,
    make_expression,
)
from iam.apply.models import (
    ActionWithoutResources,
    ActionWithResources,
    Application,
    RelatedResourceType,
    ResourceInstance,
    ResourceNode,
)
from iam.exceptions import AuthAPIError
from iam.meta import setup_action, setup_resource, setup_system

from apps.iam.exceptions import (
    ActionNotExistError,
    GetSystemInfoError,
    PermissionDeniedError,
)
from apps.iam.handlers.actions import ActionMeta, _all_actions, get_action_by_id
from apps.iam.handlers.compatible import CompatibleIAM
from apps.iam.handlers.resources import Business as BusinessResource
from apps.iam.handlers.resources import ResourceEnum, _all_resources, get_resource_by_id
from apps.iam.utils import gen_perms_apply_data
from apps.utils.local import get_request, get_request_username, get_local_username
from apps.utils.log import logger


class Permission:
    """
    权限中心鉴权封装
    """

    def __init__(self, username: str = "", bk_tenant_id: str = "", request=None):
        if username and bk_tenant_id:
            self.username = username
            self.bk_tenant_id = bk_tenant_id
        else:
            try:
                request = request or get_request(peaceful=True)
                # web请求
                if request:
                    self.username = request.user.username
                    self.bk_tenant_id = request.user.tenant_id
                else:
                    self.bk_tenant_id = settings.DEFAULT_TENANT_ID
                    logger.warning(
                        "IAM Permission init with local username, use default bk_tenant_id: %s", self.bk_tenant_id
                    )
                    # 后台设置
                    self.username = get_local_username()
                    if self.username is None:
                        raise ValueError("must provide `username` or `request` param to init")
            except Exception:  # pylint: disable=broad-except
                self.username = get_request_username()

        self.iam_client = self.get_iam_client(bk_tenant_id)
        # 是否跳过权限中心校验
        # 如果request header 中携带token，通过获取token中的鉴权类型type匹配action
        self.skip_check = getattr(settings, "SKIP_IAM_PERMISSION_CHECK", False)
        if request and getattr(request, "skip_check", False):
            self.skip_check = True

    @classmethod
    def get_iam_client(cls, bk_tenant_id: str):
        return CompatibleIAM(
            settings.APP_CODE, settings.SECRET_KEY, settings.BK_IAM_APIGATEWAY_URL, bk_tenant_id=bk_tenant_id
        )

    def make_request(self, action: ActionMeta | str, resources: list[Resource] = None) -> Request:
        """
        获取请求对象
        """
        action = get_action_by_id(action)
        resources = resources or []
        request = Request(
            system=settings.BK_IAM_SYSTEM_ID,
            subject=Subject("user", self.username),
            action=action,
            resources=resources,
            environment=None,
        )
        return request

    def make_multi_action_request(
        self, actions: list[ActionMeta | str], resources: list[Resource] = None
    ) -> MultiActionRequest:
        """
        获取多个动作请求对象
        """
        resources = resources or []
        actions = [get_action_by_id(action) for action in actions]
        request = MultiActionRequest(
            system=settings.BK_IAM_SYSTEM_ID,
            subject=Subject("user", self.username),
            actions=actions,
            resources=resources,
            environment=None,
        )
        return request

    def _make_application(
        self, action_ids: list[str], resources: list[Resource] = None, system_id: str = settings.BK_IAM_SYSTEM_ID
    ) -> Application:
        resources = resources or []
        actions = []

        for action_id in action_ids:
            # 对于没有关联资源的动作，则不传资源
            related_resources_types = []
            try:
                action = get_action_by_id(action_id)
                action_id = action.id
                related_resources_types = action.related_resource_types
            except ActionNotExistError:
                pass

            if not related_resources_types:
                actions.append(ActionWithoutResources(action_id))
            else:
                related_resources = []
                for related_resource in related_resources_types:
                    instances = []
                    for r in resources:
                        if r.system == related_resource.system_id and r.type == related_resource.id:
                            instances.append(
                                ResourceInstance(
                                    [ResourceNode(type=r.type, id=r.id, name=r.attribute.get("name", r.id))]
                                )
                            )

                    related_resources.append(
                        RelatedResourceType(
                            system_id=related_resource.system_id,
                            type=related_resource.id,
                            instances=instances,
                        )
                    )

                actions.append(ActionWithResources(action_id, related_resources))

        application = Application(system_id, actions=actions)
        return application

    def get_apply_url(
        self, action_ids: list[str], resources: list[Resource] = None, system_id: str = settings.BK_IAM_SYSTEM_ID
    ):
        """
        处理无权限 - 跳转申请列表
        """
        application = self._make_application(action_ids, resources, system_id)
        ok, message, url = self.iam_client.get_apply_url(application)
        if not ok:
            logger.error(f"iam generate apply url fail: {message}")
            return settings.BK_IAM_SAAS_HOST
        return url

    def get_apply_data(self, actions: list[ActionMeta | str], resources: list[Resource] = None):
        """
        生成本系统无权限数据
        """
        resources = resources or []
        # # 获取关联的动作，如果没有权限就一同显示
        # related_actions = fetch_related_actions(actions)
        #
        # if related_actions:
        #     request = self.make_multi_action_request(list(related_actions.values()), resources)
        #     related_actions_result = self.iam_client.resource_multi_actions_allowed(request)
        #
        #     for action_id, is_allowed in related_actions_result.items():
        #         if not is_allowed and action_id in related_actions:
        #             actions.append(related_actions[action_id])

        action_to_resources_list = []
        for action in actions:
            action = get_action_by_id(action)

            if not action.related_resource_types:
                # 如果没有关联资源，则直接置空
                resources = []

            action_to_resources_list.append({"action": action, "resources_list": [resources]})

        self.setup_meta()

        data = gen_perms_apply_data(
            system=settings.BK_IAM_SYSTEM_ID,
            subject=Subject("user", self.username),
            action_to_resources_list=action_to_resources_list,
        )

        url = self.get_apply_url(actions, resources)
        return data, url

    @staticmethod
    def is_demo_biz_resource(resources: list[Resource] = None):
        """
        判断资源是否为demo业务的资源
        """
        if not settings.DEMO_BIZ_ID:
            return False
        if not resources:
            return False
        if not len(resources) == 1:
            return False
        if (resources[0].system, resources[0].type, str(resources[0].id)) == (
            BusinessResource.system_id,
            BusinessResource.id,
            str(settings.DEMO_BIZ_ID),
        ):
            # 业务类型资源判断资源ID
            return True
        if resources[0].attribute and resources[0].attribute.get("_bk_iam_path_", "").startswith(
            f"/biz,{settings.DEMO_BIZ_ID}/"
        ):
            # 其他类型资源，判断路径
            return True
        return False

    def is_allowed(self, action: ActionMeta | str, resources: list[Resource] = None, raise_exception: bool = False):
        """
        校验用户是否有动作的权限
        :param action: 动作
        :param resources: 依赖的资源实例列表
        :param raise_exception: 鉴权失败时是否需要抛出异常
        """
        action = get_action_by_id(action)
        if not action.related_resource_types:
            resources = []

        # ===== 针对demo业务的权限豁免 开始 ===== #
        if self.is_demo_biz_resource(resources):
            # 如果是demo业务，则进行权限豁免，分为读写权限
            if settings.DEMO_BIZ_EDIT_ENABLED or action.is_read_action():
                return True
        # ===== 针对demo业务的权限豁免 结束 ===== #

        request = self.make_request(action, resources)

        try:
            result = self.iam_client.is_allowed(request)
        except AuthAPIError as e:
            logger.exception(f"[IAM AuthAPI Error]: {e}")
            result = False

        if not result and raise_exception:
            apply_data, apply_url = self.get_apply_data([action], resources)
            raise PermissionDeniedError(
                action_name=action.name,
                apply_url=apply_url,
                permission=apply_data,
            )

        return result

    def is_allowed_by_biz(self, bk_biz_id: int, action: ActionMeta | str, raise_exception: bool = False):
        """
        判断用户对当前动作在该业务下是否有权限
        """
        if self.skip_check:
            return True

        resources = [ResourceEnum.BUSINESS.create_simple_instance(bk_biz_id)]
        return self.is_allowed(action, resources, raise_exception)

    def batch_is_allowed(self, actions: list[ActionMeta], resources: list[list[Resource]]):
        """
        查询某批资源某批操作是否有权限
        """
        request = self.make_multi_action_request(actions)
        result = self.iam_client.batch_resource_multi_actions_allowed(request, resources)

        # ===== 针对demo业务的权限豁免 开始 ===== #
        for action in actions:
            if not settings.DEMO_BIZ_EDIT_ENABLED and not action.is_read_action():
                continue
            for resource in resources:
                resource_id = resource[0].id
                action_id = action.id
                if self.is_demo_biz_resource(resource) and resource_id in result and action_id in result[resource_id]:
                    result[resource_id][action_id] = True
        # ===== 针对demo业务的权限豁免 结束 ===== #

        return result

    @classmethod
    def make_resource(cls, resource_type: str, instance_id: str) -> Resource:
        """
        构造resource对象
        :param resource_type: 资源类型
        :param instance_id: 实例ID
        """
        resource_meta = get_resource_by_id(resource_type)
        return resource_meta.create_instance(instance_id)

    @classmethod
    def batch_make_resource(cls, resources: list[dict]):
        """
        批量构造resource对象
        """
        return [cls.make_resource(r["type"], r["id"]) for r in resources]

    def get_system_info(self):
        """
        获取权限中心注册的动作列表
        """
        ok, message, data = self.iam_client._client.query(settings.BK_IAM_SYSTEM_ID)
        if not ok:
            raise GetSystemInfoError(_("获取系统信息错误：{message}").format(message))
        return data

    def filter_space_list_by_action(
        self, action: ActionMeta | str, bk_tenant_id: str = "", space_list: list = None
    ) -> list:
        """
        根据动作过滤用户有权限的业务列表
        """
        if space_list is None:
            # 获取业务列表
            from apps.log_search.models import Space

            space_list = Space.get_all_spaces(bk_tenant_id=bk_tenant_id)
        # 跳过权限检验
        if settings.IGNORE_IAM_PERMISSION:
            return space_list

        # 拉取策略
        request = self.make_request(action=action)

        try:
            policies = self.iam_client._do_policy_query(request)
        except AuthAPIError as e:
            logger.exception(f"[IAM AuthAPI Error]: {e}")
            return []

        if not policies:
            # 如果策略是空，则说明没有任何权限，若存在Demo业务，返回Demo业务，否则返回空
            for space in space_list:
                if settings.DEMO_BIZ_ID == space["bk_biz_id"]:
                    return [space]
            return []

        # 生成表达式
        expr = make_expression(policies)

        results = []
        for space in space_list:
            obj_set = ObjectSet()
            obj_set.add_object(_type=ResourceEnum.BUSINESS.id, obj={"id": str(space["bk_biz_id"])})

            # 计算表达式
            is_allowed = self.iam_client._eval_expr(expr, obj_set)

            # 针对demo业务权限豁免
            if is_allowed or str(settings.DEMO_BIZ_ID) == str(space["bk_biz_id"]):
                results.append(space)

        return results

    @classmethod
    def setup_meta(cls):
        """
        初始化权限中心实体
        """
        if getattr(cls, "__setup", False):
            return

        # 系统
        systems = [
            {"system_id": settings.BK_IAM_SYSTEM_ID, "system_name": settings.BK_IAM_SYSTEM_NAME},
            {"system_id": "bk_monitorv3", "system_name": _("监控平台")},
        ]

        for system in systems:
            setup_system(**system)

        # 资源
        for r in _all_resources.values():
            setup_resource(r.system_id, r.id, r.name)

        # 动作
        for action in _all_actions.values():
            setup_action(system_id=settings.BK_IAM_SYSTEM_ID, action_id=action.id, action_name=action.name)

        cls.__setup = True

    def grant_creator_action(self, resource: Resource, creator: str = None, raise_exception=False):
        """
        新建实例关联权限授权
        :param resource: 资源实例
        :param creator: 资源创建者
        :param raise_exception: 是否抛出异常
        :return:
        """
        application = {
            "system": resource.system,
            "type": resource.type,
            "id": resource.id,
            "name": resource.attribute.get("name", resource.id) if resource.attribute else resource.id,
            "creator": creator or self.username,
        }

        grant_result = None

        try:
            grant_result = self.iam_client.grant_resource_creator_actions(application)
            logger.info(f"[grant_creator_action] Success! resource: {resource.to_dict()}, result: {grant_result}")
        except Exception as e:  # pylint: disable=broad-except
            logger.exception(f"[grant_creator_action] Failed! resource: {resource.to_dict()}, result: {e}")

            if raise_exception:
                raise e

        return grant_result
