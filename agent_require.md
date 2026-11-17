本文档主要给agent使用的，agent需要根据本文档的要求进行开发。

# md文档要求

|  要求一： |
|  --- |
|  md文档职责要清晰，每个文档只负责某一单一的功能；比如：readme文档要简洁，readme主要是给用户使用的，说清楚本系统是干什么的，有什么功能就行了，至于如何部署配置walle服务放到部署文档里，至于业务的实现细节也不要写在这个文档里，写到 walle服务端详细设计 文档里。 |
| 要求二：|
｜｜



# 目录结构
```bash
├── docs/             # 文档
├── tests/            # 测试
├── scripts/          # 构建、备份、发布脚本
├── etc/              # 运行期配置：app.yaml、nginx.conf
├── src/              # 源码
│     ├── web/        # web端源码
│     └── backend/    # 后端源码
├── thirdparty/       # 第三方代码
├── README.md
└── LICENSE
```




# 代码要求



# git相关要求
|  要求一： |
|  --- |
|  每次修改完代码后，根据docs/version_record.md 的要求，编写commit的内容，但是不提交，由我确认后，再提交到dev分支。 |

