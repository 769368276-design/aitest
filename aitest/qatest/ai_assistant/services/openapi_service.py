import json
import yaml
from typing import Dict, List, Any, Optional, Union
from datetime import datetime
import re

class OpenAPIService:
    def __init__(self):
        self.supported_extensions = ['.json', '.yaml', '.yml']
    
    def parse_openapi_file(self, file_path: str) -> Dict[str, Any]:
        """
        解析OpenAPI/Swagger文件
        """
        try:
            # 根据文件扩展名选择解析方法
            if file_path.endswith('.json'):
                with open(file_path, 'r', encoding='utf-8') as f:
                    api_spec = json.load(f)
            elif file_path.endswith(('.yaml', '.yml')):
                with open(file_path, 'r', encoding='utf-8') as f:
                    api_spec = yaml.safe_load(f)
            else:
                raise ValueError("不支持的文件格式，请使用JSON或YAML格式")
            
            # 解析API规范
            parsed_api = self._parse_api_specification(api_spec)
            
            return {
                'api_info': parsed_api,
                'parsed_at': datetime.now().isoformat(),
                'file_path': file_path
            }
            
        except Exception as e:
            raise Exception(f"OpenAPI文件解析失败: {str(e)}")
    
    def _parse_api_specification(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        解析API规范内容
        """
        parsed = {
            'info': {},
            'servers': [],
            'paths': [],
            'components': {},
            'security': [],
            'tags': []
        }
        
        # 解析基本信息
        if 'info' in spec:
            parsed['info'] = {
                'title': spec['info'].get('title', ''),
                'description': spec['info'].get('description', ''),
                'version': spec['info'].get('version', ''),
                'contact': spec['info'].get('contact', {}),
                'license': spec['info'].get('license', {})
            }
        
        # 解析服务器信息
        if 'servers' in spec:
            parsed['servers'] = spec['servers']
        elif 'host' in spec:  # Swagger 2.0格式
            parsed['servers'] = [{
                'url': f"{spec.get('schemes', ['http'])[0]}://{spec['host']}{spec.get('basePath', '')}",
                'description': 'Default server'
            }]
        
        # 解析路径和操作
        if 'paths' in spec:
            for path, path_item in spec['paths'].items():
                parsed_path = {
                    'path': path,
                    'operations': []
                }
                
                # 解析每个HTTP方法
                for method, operation in path_item.items():
                    if method.lower() in ['get', 'post', 'put', 'delete', 'patch', 'options', 'head']:
                        parsed_operation = self._parse_operation(method, operation, path)
                        parsed_path['operations'].append(parsed_operation)
                
                if parsed_path['operations']:
                    parsed['paths'].append(parsed_path)
        
        # 解析组件（schemas, responses等）
        if 'components' in spec:
            parsed['components'] = spec['components']
        elif 'definitions' in spec:  # Swagger 2.0格式
            parsed['components'] = {'schemas': spec['definitions']}
        
        # 解析安全定义
        if 'security' in spec:
            parsed['security'] = spec['security']
        
        # 解析标签
        if 'tags' in spec:
            parsed['tags'] = spec['tags']
        
        return parsed
    
    def _parse_operation(self, method: str, operation: Dict[str, Any], path: str) -> Dict[str, Any]:
        """
        解析单个API操作
        """
        parsed_op = {
            'method': method.upper(),
            'path': path,
            'operation_id': operation.get('operationId', f"{method}_{path.replace('/', '_')}"),
            'summary': operation.get('summary', ''),
            'description': operation.get('description', ''),
            'tags': operation.get('tags', []),
            'parameters': [],
            'request_body': {},
            'responses': {},
            'security': operation.get('security', [])
        }
        
        # 解析参数
        if 'parameters' in operation:
            for param in operation['parameters']:
                parsed_param = {
                    'name': param.get('name', ''),
                    'in': param.get('in', ''),
                    'description': param.get('description', ''),
                    'required': param.get('required', False),
                    'schema': param.get('schema', param.get('type', {})),
                    'example': param.get('example', '')
                }
                parsed_op['parameters'].append(parsed_param)
        
        # 解析请求体
        if 'requestBody' in operation:
            parsed_op['request_body'] = operation['requestBody']
        elif method.upper() in ['POST', 'PUT', 'PATCH']:
            # 检查parameters中的body参数（Swagger 2.0）
            for param in operation.get('parameters', []):
                if param.get('in') == 'body':
                    parsed_op['request_body'] = {
                        'description': param.get('description', ''),
                        'content': {
                            'application/json': {
                                'schema': param.get('schema', {})
                            }
                        }
                    }
        
        # 解析响应
        if 'responses' in operation:
            for status_code, response in operation['responses'].items():
                parsed_op['responses'][status_code] = {
                    'description': response.get('description', ''),
                    'content': response.get('content', {}),
                    'headers': response.get('headers', {}),
                    'schema': response.get('schema', {})  # Swagger 2.0
                }
        
        return parsed_op
    
    def generate_test_scenarios(self, api_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        基于API规范生成测试场景
        """
        test_scenarios = []
        
        for path_info in api_info.get('paths', []):
            for operation in path_info.get('operations', []):
                scenarios = self._generate_operation_test_scenarios(operation)
                test_scenarios.extend(scenarios)
        
        return test_scenarios
    
    def _generate_operation_test_scenarios(self, operation: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        为单个API操作生成测试场景
        """
        scenarios = []
        base_scenario = {
            'api_method': operation['method'],
            'api_path': operation['path'],
            'operation_id': operation['operation_id'],
            'summary': operation['summary'],
            'description': operation['description']
        }
        
        # 正向测试场景
        positive_scenario = {
            **base_scenario,
            'scenario_type': 'positive',
            'test_case_title': f"测试 {operation['method']} {operation['path']} - 正常流程",
            'test_description': f"验证 {operation['summary']} 的正常功能",
            'test_data': self._generate_positive_test_data(operation),
            'expected_responses': self._get_success_responses(operation)
        }
        scenarios.append(positive_scenario)
        
        # 负向测试场景
        negative_scenarios = self._generate_negative_scenarios(operation, base_scenario)
        scenarios.extend(negative_scenarios)
        
        # 边界测试场景
        boundary_scenarios = self._generate_boundary_scenarios(operation, base_scenario)
        scenarios.extend(boundary_scenarios)
        
        return scenarios
    
    def _generate_positive_test_data(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成正向测试数据
        """
        test_data = {
            'path_parameters': {},
            'query_parameters': {},
            'header_parameters': {},
            'request_body': {}
        }
        
        for param in operation.get('parameters', []):
            param_name = param['name']
            param_location = param['in']
            
            # 根据参数类型生成示例数据
            example_value = self._generate_example_value(param)
            
            if param_location == 'path':
                test_data['path_parameters'][param_name] = example_value
            elif param_location == 'query':
                test_data['query_parameters'][param_name] = example_value
            elif param_location == 'header':
                test_data['header_parameters'][param_name] = example_value
        
        # 生成请求体示例
        if operation.get('request_body'):
            test_data['request_body'] = self._generate_request_body_example(operation['request_body'])
        
        return test_data
    
    def _generate_example_value(self, param: Dict[str, Any]) -> Any:
        """
        根据参数定义生成示例值
        """
        if 'example' in param and param['example']:
            return param['example']
        
        # 处理 schema 可能是字符串的情况
        schema = param.get('schema', {})
        if isinstance(schema, str):
            param_type = schema
        elif isinstance(schema, dict):
            param_type = schema.get('type', param.get('type', 'string'))
        else:
            param_type = param.get('type', 'string')
        
        if param_type == 'string':
            if isinstance(schema, dict) and 'enum' in schema:
                return schema['enum'][0]
            return f"example_{param['name']}"
        elif param_type == 'integer':
            return 123
        elif param_type == 'number':
            return 123.45
        elif param_type == 'boolean':
            return True
        elif param_type == 'array':
            return ["example_item"]
        else:
            return f"example_{param['name']}"
    
    def _generate_request_body_example(self, request_body: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成请求体示例
        """
        # 简化实现，实际应该根据schema生成
        return {"example": "request_body"}
    
    def _get_success_responses(self, operation: Dict[str, Any]) -> List[str]:
        """
        获取成功响应状态码
        """
        success_codes = []
        for status_code in operation.get('responses', {}):
            if status_code.startswith('2'):  # 2xx状态码
                success_codes.append(status_code)
        return success_codes or ['200']
    
    def _generate_negative_scenarios(self, operation: Dict[str, Any], base_scenario: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        生成负向测试场景
        """
        scenarios = []
        
        # 缺少必需参数的场景
        required_params = [p for p in operation.get('parameters', []) if p.get('required', False)]
        if required_params:
            scenario = {
                **base_scenario,
                'scenario_type': 'negative',
                'test_case_title': f"测试 {operation['method']} {operation['path']} - 缺少必需参数",
                'test_description': "验证缺少必需参数时的错误处理",
                'expected_responses': ['400', '422']
            }
            scenarios.append(scenario)
        
        # 无效数据类型场景
        scenario = {
            **base_scenario,
            'scenario_type': 'negative',
            'test_case_title': f"测试 {operation['method']} {operation['path']} - 无效数据类型",
            'test_description': "验证传入无效数据类型时的错误处理",
            'expected_responses': ['400', '422']
        }
        scenarios.append(scenario)
        
        return scenarios
    
    def _generate_boundary_scenarios(self, operation: Dict[str, Any], base_scenario: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        生成边界测试场景
        """
        scenarios = []
        
        # 最大长度测试
        string_params = []
        for p in operation.get('parameters', []):
            # 处理 schema 可能是字符串的情况
            schema = p.get('schema', {})
            if isinstance(schema, str):
                param_type = schema
            elif isinstance(schema, dict):
                param_type = schema.get('type', p.get('type', 'string'))
            else:
                param_type = p.get('type', 'string')
            
            if param_type == 'string':
                string_params.append(p)
        
        if string_params:
            scenario = {
                **base_scenario,
                'scenario_type': 'boundary',
                'test_case_title': f"测试 {operation['method']} {operation['path']} - 边界值测试",
                'test_description': "验证参数边界值的处理",
                'expected_responses': ['200', '400']
            }
            scenarios.append(scenario)
        
        return scenarios

openapi_service = OpenAPIService()
