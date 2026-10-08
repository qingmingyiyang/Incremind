# GeoNames 城市与居民点离线资源

资源来源：[GeoNames cities500](https://download.geonames.org/export/dump/cities500.zip)，字段说明见[官方说明](https://download.geonames.org/export/dump/readme.txt)和[地物代码](https://www.geonames.org/export/codes.html)。
GeoNames 数据采用 [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/)，原始资料按许可所述提供，不作保证。
本项目将原始数据筛选并转为 JSON/gzip，保留地名标识、名称、国家和官方别名。

冻结日期：2026-10-07 UTC。原始档案 13868902 字节，读取 236003 条。
本资源保留 224552 个地名标识、1077628 个不区分大小写的别名，其中 73242 个别名对应多个地名。
输出 8746895 字节；通过 19 字段检查、标识唯一性和 gzip/JSON 往返结构检查。
入表地物代码数量：{"PPL": 139596, "PPLA": 3464, "PPLA2": 23370, "PPLA3": 30528, "PPLA4": 27353, "PPLC": 241}。
排除地物代码数量：{"PPLA5": 30, "PPLCH": 1, "PPLF": 47, "PPLG": 9, "PPLH": 27, "PPLL": 600, "PPLQ": 41, "PPLR": 4, "PPLS": 45, "PPLW": 9, "PPLX": 10562, "STLMT": 48}。

筛选限于地物类 P 中的 PPL、PPLC、PPLA、PPLA2、PPLA3、PPLA4，即当前居民点、首都及一至四级行政驻地。
覆盖官方 cities500 集合内的城市、城镇和部分居民点，沿用原集合范围，不另造人口阈值。
不包含全部城市或全部地名，也不包含街区、历史或废弃聚落（例如 PPLX、PPLH、PPLQ）。
别名来自 name、asciiname、alternatenames，不自行生成译名；排除空名、纯数字、少于两个字符的名称、短字母缩写。
因此排除 28 个在短名及缩写筛选后没有可用别名的居民点；这些地名属于资源覆盖边界。
同名多个地名的别名由调用策略拒绝；资料中没有国家限定时不会猜选其中一个。
匹配完全在本机进行，不上传位置、场景名或资料正文，不在线查询地名。
