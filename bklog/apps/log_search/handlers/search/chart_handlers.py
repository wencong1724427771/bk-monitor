# -*- coding: utf-8 -*-
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
import re

from django.utils.module_loading import import_string
from django.utils.translation import ugettext as _

from apps.api import BkDataQueryApi
from apps.log_search.constants import SQL_PREFIX, SQL_SUFFIX, SearchMode
from apps.log_search.exceptions import (
    BaseSearchIndexSetException,
    IndexSetDorisQueryException,
    SQLQueryException,
)
from apps.log_search.models import LogIndexSet
from apps.utils.log import logger


class ChartHandler(object):
    def __init__(self, index_set_id):
        self.index_set_id = index_set_id
        try:
            self.data = LogIndexSet.objects.get(index_set_id=self.index_set_id)
        except LogIndexSet.DoesNotExist:
            raise BaseSearchIndexSetException(
                BaseSearchIndexSetException.MESSAGE.format(index_set_id=self.index_set_id)
            )

    @classmethod
    def get_instance(cls, index_set_id, mode):
        mapping = {
            SearchMode.UI.value: "UIChartHandler",
            SearchMode.SQL.value: "SQLChartHandler",
        }
        try:
            chart_instance = import_string(
                "apps.log_search.handlers.search.chart_handlers.{}".format(mapping.get(mode))
            )
            return chart_instance(index_set_id=index_set_id)
        except ImportError as error:
            raise NotImplementedError(f"{mode} class not implement, error: {error}")

    def get_chart_data(self, params: dict) -> dict:
        """
        获取图表相关信息
        :param params: 图表参数
        :return: 图表数据 dict
        """
        raise NotImplementedError(_("功能暂未实现"))

    @staticmethod
    def generate_sql(params: dict) -> str:
        """
        根据过滤条件生成sql
        :param params: 过滤条件
        """
        start_time = params["start_time"] * 1000
        end_time = params["end_time"] * 1000

        sql = f"WHERE dtEventTimeStamp>={start_time} AND dtEventTimeStamp<={end_time}"
        addition = params["addition"]

        for condition in addition:
            sql += " AND "
            field_name = condition["field"]
            operator = condition["operator"]
            values = condition["value"]

            if operator in ["is true", "is false"]:
                sql += f"{field_name} {operator.upper()}"
                continue

            # values 不为空时才走后面的逻辑
            if not values:
                continue

            # 组内条件的与或关系
            condition_type = "OR"
            if operator in ["&=~", "&!=~", "all contains match phrase", "all not contains match phrase"]:
                condition_type = "AND"

            # 日志的操作符转化为sql操作符
            sql_operator = operator
            if operator in ["=~", "&=~", "contains"]:
                sql_operator = "LIKE"
            elif operator in ["!=~", "&!=~", "not contains"]:
                sql_operator = "NOT LIKE"
            elif operator in ["contains match phrase", "all contains match phrase"]:
                sql_operator = "MATCH_ANY"
            elif operator in ["not contains match phrase", "all not contains match phrase"]:
                sql_operator = "NOT MATCH_ANY"

            tmp_sql = ""
            for index, value in enumerate(values):
                if operator in ["=~", "&=~", "!=~", "&!=~"]:
                    # 替换通配符
                    value = value.replace("*", "%")
                    value = value.replace("?", "_")
                elif operator in ["contains", "not contains"]:
                    # 添加通配符
                    value = f"%{value}%"

                if index > 0:
                    tmp_sql += f" {condition_type} "
                if not isinstance(value, int):
                    value = f"\'{value}\'"
                tmp_sql += f"{field_name} {sql_operator} {value}"

            # 有两个以上的值,且是OR关系是才需要加括号
            sql += tmp_sql if condition_type == "AND" or len(values) == 1 else ("(" + tmp_sql + ")")
        return SQL_PREFIX + f" {sql} " + SQL_SUFFIX


class UIChartHandler(ChartHandler):
    def get_chart_data(self, params: dict) -> dict:
        """
        UI模式获取图表相关信息
        :param params: 图表参数
        :return: 图表数据 dict
        """
        # TODO 待实现
        return {}


class SQLChartHandler(ChartHandler):
    def get_chart_data(self, params) -> dict:
        """
        Sql模式获取图表相关信息
        :param params: 图表参数
        :return: 图表数据 dict
        """
        if not self.data.support_doris:
            raise IndexSetDorisQueryException()
        parsed_sql = self.parse_sql_syntax(self.data.doris_table_id, params["sql"])
        data = self.fetch_query_data(parsed_sql)
        return data

    @staticmethod
    def parse_sql_syntax(doris_table_id: str, raw_sql: str):
        """
        解析sql语法
        """
        # 如果不存在FROM则添加,存在则覆盖
        pattern = (
            r"^\s*?(SELECT\s+?.+?)"
            r"(?:\bFROM\b.+?)?"
            r"(\bWHERE\b.*|\bGROUP\s+?BY\b.*|\bHAVING\b.*|\bORDER\s+?BY\b.*|\bINTO\s+?OUTFILE\b.*)?$"
        )
        matches = re.match(pattern, raw_sql, re.DOTALL | re.IGNORECASE)
        if not matches:
            raise SQLQueryException(SQLQueryException.MESSAGE.format(name=_("缺少SQL查询的关键字")))
        parsed_sql = matches.group(1) + f" FROM {doris_table_id} "
        if matches.group(2):
            parsed_sql += matches.group(2)
        return parsed_sql

    @staticmethod
    def fetch_query_data(sql: str) -> dict:
        """
        获取查询结果
        :param sql: 查询sql
        :return: 查询结果 dict
        """
        result_data = BkDataQueryApi.query({"sql": sql}, raw=True)
        result = result_data.get("result")
        if not result:
            # SQL查询失败, 抛出异常
            errors_message = result_data.get("message", {})
            errors = result_data.get("errors", {}).get("error")
            if errors:
                errors_message = errors_message + ":" + errors
            logger.info("SQL query exception [%s]", errors_message)
            raise SQLQueryException(SQLQueryException.MESSAGE.format(name=errors_message))

        data_list = result_data["data"]["list"]
        result_schema = result_data["data"].get("result_schema", [])
        index = 0
        # 接口中不存在时,构造result_schema
        if not result_schema and data_list:
            for key, value in data_list[0].items():
                if key in ["dtEventTimeStamp", "dtEventTime", "time"]:
                    field_type = "date"
                elif isinstance(value, int):
                    field_type = "long"
                elif isinstance(value, float):
                    field_type = "double"
                else:
                    field_type = "string"
                result_schema.append(
                    {"field_type": field_type, "field_name": key, "field_alias": key, "field_index": index}
                )
                index += 1
        data = {
            "total_records": result_data["data"]["totalRecords"],
            "time_taken": result_data["data"]["timetaken"],
            "list": data_list,
            "select_fields_order": result_data["data"]["select_fields_order"],
            "result_schema": result_schema,
        }
        return data