"""
测试数据准备 —— 从原始 CSV 划分子集、构造脏数据
================================================
"""
from __future__ import annotations

import csv
import random
import json
from pathlib import Path


def extract_subset(
    source_csv: str,  # 原始CSV文件路径
    n_ships: int,  # 要提取的船舶数量
    output_path: str,  # 输出CSV文件路径
    seed: int = 42,  # 随机种子，确保可复现
):
    """
    从原始 CSV 中提取 N 艘船的数据子集。

    实现方式：
        1. 读取原始 CSV，按 MMSI 分组
        2. 随机抽取 N 个 MMSI
        3. 提取这些 MMSI 的所有行，写入新 CSV

    参数:
        source_csv: 原始 CSV 路径
        n_ships: 要提取的船数
        output_path: 输出路径
        seed: 随机种子
    """
    rng = random.Random(seed)  # 可复现的随机数生成器

    # 按 MMSI 分组
    # 注意：原始 CSV 可能列名大写（MMSI, TIME...），统一转小写匹配
    ships: dict[str, list[dict]] = {}  # MMSI到行列表的映射字典
    with open(source_csv, encoding="utf-8-sig") as f:  # 打开原始CSV文件（含BOM头）
        reader = csv.DictReader(f)  # CSV字典读取器
        # 统一列名为小写
        lower_fieldnames = reader.fieldnames or []  # 小写的字段名列表
        lower_fieldnames = [c.strip().lower() for c in lower_fieldnames]  # 去除空白并转小写
        # 构建小写→原始列名映射
        col_map = {orig.strip().lower(): orig.strip() for orig in lower_fieldnames}  # 小写到原始列名的映射字典
        # 重建所有行为小写 key
        rows_lower = []  # 所有行（键已转小写）的列表
        for row in reader:  # 遍历CSV每一行
            lower_row = {k.strip().lower(): v for k, v in row.items()}  # 当前行键转小写
            rows_lower.append(lower_row)  # 添加到列表
        fieldnames = lower_fieldnames  # 最终使用的字段名列表

        for row in rows_lower:  # 遍历所有行
            mmsi = row.get("mmsi", "").strip()  # 当前行的MMSI标识
            if mmsi:  # 如果MMSI非空
                if mmsi not in ships:  # 如果该MMSI尚未记录
                    ships[mmsi] = []  # 初始化该MMSI的行列表
                ships[mmsi].append(row)  # 将当前行加入对应MMSI的分组

    all_mmsi = list(ships.keys())  # 所有唯一的MMSI列表

    if n_ships >= len(all_mmsi):  # 如果请求船数大于等于实际船数
        selected_mmsi = all_mmsi  # 使用全部MMSI
    else:
        selected_mmsi = rng.sample(all_mmsi, n_ships)  # 随机抽取指定数量的MMSI

    # 写入新 CSV
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在
    count = 0  # 写入行数计数器
    with open(output_path, "w", encoding="utf-8", newline="") as f:  # 打开输出CSV文件
        writer = csv.DictWriter(f, fieldnames=fieldnames)  # CSV字典写入器
        writer.writeheader()  # 写入表头
        for mmsi in selected_mmsi:  # 遍历选中的MMSI
            for row in ships[mmsi]:  # 遍历该MMSI的所有行
                writer.writerow(row)  # 写入行数据
                count += 1  # 行数加1

    return {
        "source": source_csv,  # 原始CSV路径
        "output": output_path,  # 输出CSV路径
        "requested_ships": n_ships,  # 请求的船舶数量
        "actual_ships": len(selected_mmsi),  # 实际提取的船舶数量
        "total_rows": count,  # 总行数
    }


def prepare_test_subsets(
    source_csv: str,  # 原始CSV文件路径
    subset_dir: str,  # 子集输出目录
    ship_counts: list[int] | None = None,  # 各子集的船数列表，默认为[1,10,50,100,200]
) -> list[dict]:
    """
    准备多个规模的测试子集。

    参数:
        source_csv: 原始 CSV
        subset_dir: 子集输出目录
        ship_counts: 各子集的船数，默认 [1, 10, 50, 100, 200]

    返回:
        list[dict]: 每个子集的描述
    """
    if ship_counts is None:  # 如果未指定船数列表
        ship_counts = [1, 10, 50, 100, 200]  # 使用默认船数列表

    results = []  # 结果列表
    for n in ship_counts:  # 遍历每种船数
        out = Path(subset_dir) / f"test_{n}ships.csv"  # 构建输出文件路径
        info = extract_subset(source_csv, n, str(out))  # 提取子集并获取信息
        results.append(info)  # 追加到结果列表
        print(f"  [数据] 已生成: {out.name} ({info['actual_ships']} 船, {info['total_rows']} 行)")

    return results


def create_dirty_csv(
    source_csv: str,  # 原始CSV文件路径
    fault_type: str,  # 异常类型（empty/missing_fields/extreme_values等）
    output_path: str,  # 输出脏数据CSV文件路径
):
    """
    从原始 CSV 构造带特定异常的脏数据文件。

    支持的异常类型:
        "empty"            - 仅有表头，无数据
        "missing_fields"   - 删除 mmsi/lon/lat 列
        "extreme_values"   - speed=-999, lon=999
        "time_anomaly"     - 所有行时间戳相同
        "encoding_error"   - GBK 编码代替 UTF-8
        "corrupted_lat"    - lat 写非法字符串 "NaN"

    参数:
        source_csv: 原始 CSV
        fault_type: 异常类型
        output_path: 输出路径
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在

    with open(source_csv, encoding="utf-8-sig") as f:  # 打开原始CSV文件（含BOM头）
        reader = csv.DictReader(f)  # CSV字典读取器
        lower_fieldnames = reader.fieldnames  # 获取原始字段名
        # 统一转为小写列名（兼容原始 CSV 大写列名）
        lower_fieldnames = [c.strip().lower() for c in (lower_fieldnames or [])]  # 字段名转小写
        # 读取所有行并转为小写 key
        original_rows = []  # 原始数据行列表
        for row in reader:  # 遍历CSV每一行
            original_rows.append({k.strip().lower(): v for k, v in row.items()})  # 添加键转小写的行

    if fault_type == "empty":
        # 只有表头
        with open(output_path, "w", encoding="utf-8", newline="") as f:  # 打开输出文件
            writer = csv.DictWriter(f, fieldnames=lower_fieldnames)  # CSV字典写入器
            writer.writeheader()  # 仅写入表头
        return {
            "name": "空文件（仅表头）",  # 异常类型名称
            "output": output_path,  # 输出文件路径
            "row_count": 0,  # 数据行数
        }

    elif fault_type == "missing_fields":
        # 仅保留 mmsi 和 time
        keep = ["mmsi", "time"]  # 保留的字段列表
        fieldnames = [f for f in lower_fieldnames if f in keep]  # 过滤后的字段名列表
        with open(output_path, "w", encoding="utf-8", newline="") as f:  # 打开输出文件
            writer = csv.DictWriter(f, fieldnames=fieldnames)  # CSV字典写入器
            writer.writeheader()  # 写入表头
            for row in original_rows:  # 遍历原始行
                writer.writerow({k: row.get(k, "") for k in fieldnames})  # 仅写入保留字段
        return {
            "name": "缺失关键字段（仅留 mmsi, time）",  # 异常类型名称
            "output": output_path,  # 输出文件路径
            "row_count": len(original_rows),  # 数据行数
        }

    elif fault_type == "extreme_values":
        with open(output_path, "w", encoding="utf-8", newline="") as f:  # 打开输出文件
            writer = csv.DictWriter(f, fieldnames=lower_fieldnames)  # CSV字典写入器
            writer.writeheader()  # 写入表头
            for row in original_rows:  # 遍历原始行
                row["speed"] = "-999"  # 速度设为极值
                row["lon"] = "999"  # 经度设为极值
                row["lat"] = "999"  # 纬度设为极值
                writer.writerow(row)  # 写入行
        return {
            "name": "极值数据（speed=-999, lon=lat=999）",  # 异常类型名称
            "output": output_path,  # 输出文件路径
            "row_count": len(original_rows),  # 数据行数
        }

    elif fault_type == "time_anomaly":
        with open(output_path, "w", encoding="utf-8", newline="") as f:  # 打开输出文件
            writer = csv.DictWriter(f, fieldnames=lower_fieldnames)  # CSV字典写入器
            writer.writeheader()  # 写入表头
            for row in original_rows:  # 遍历原始行
                row["time"] = "20000101000000"  # 时间戳全部设为同一值
                writer.writerow(row)  # 写入行
        return {
            "name": "时间异常（全部为 2000-01-01）",  # 异常类型名称
            "output": output_path,  # 输出文件路径
            "row_count": len(original_rows),  # 数据行数
        }

    elif fault_type == "encoding_error":
        # 用 GBK 写入（实际上包含无法编码的字符时会出错）
        with open(output_path, "w", encoding="gbk", errors="replace", newline="") as f:  # 以GBK编码打开输出文件
            writer = csv.DictWriter(f, fieldnames=lower_fieldnames)  # CSV字典写入器
            writer.writeheader()  # 写入表头
            for row in original_rows:  # 遍历原始行
                writer.writerow(row)  # 写入行
        return {
            "name": "编码异常（GBK 编码）",  # 异常类型名称
            "output": output_path,  # 输出文件路径
            "row_count": len(original_rows),  # 数据行数
        }

    elif fault_type == "corrupted_lat":
        with open(output_path, "w", encoding="utf-8", newline="") as f:  # 打开输出文件
            writer = csv.DictWriter(f, fieldnames=lower_fieldnames)  # CSV字典写入器
            writer.writeheader()  # 写入表头
            for row in original_rows:  # 遍历原始行
                row["lat"] = "NaN"  # 纬度设为非法字符串
                writer.writerow(row)  # 写入行
        return {
            "name": "非法字段（lat 为 NaN）",  # 异常类型名称
            "output": output_path,  # 输出文件路径
            "row_count": len(original_rows),  # 数据行数
        }

    else:
        raise ValueError(f"未知的异常类型: {fault_type}")


def prepare_dirty_datasets(
    source_csv: str,  # 原始CSV文件路径
    dirty_dir: str,  # 脏数据输出目录
) -> list[dict]:
    """准备所有脏数据变体"""
    fault_types = [  # 要生成的异常类型列表
        "empty",
        "missing_fields",
        "extreme_values",
        "time_anomaly",
        "corrupted_lat",
    ]
    results = []  # 结果列表
    for ft in fault_types:  # 遍历每种异常类型
        out = Path(dirty_dir) / f"dirty_{ft}.csv"  # 构建脏数据输出文件路径
        info = create_dirty_csv(source_csv, ft, str(out))  # 生成脏数据并获取信息
        results.append(info)  # 追加到结果列表
        print(f"  [数据] 已生成脏数据: {out.name} ({info['name']})")
    return results


def generate_synthetic_test_set(
    output_path: str,  # 输出JSON文件路径
    n_cases: int = 100,  # 合成测试用例数量
    seed: int = 42,  # 随机种子，确保可复现
):
    """
    生成带 ground truth 的合成测试集（JSON 格式）。

    每条用例包含 ship_data + ground_truth，用于准确率对比实验。

    参数:
        output_path: 输出 JSON 路径
        n_cases: 用例数（一半正常一半违规）
        seed: 随机种子

    输出 JSON 格式:
        [
            {
                "id": "synthetic_0000",
                "ship_data": { ... },
                "ground_truth": {
                    "violation_detected": true,
                    "violation_type": "illegal_fishing",
                    "violation_label": "非法捕捞"
                },
                "note": "违规/正常"
            },
            ...
        ]
    """
    rng = random.Random(seed)  # 可复现的随机数生成器

    test_set = []  # 测试用例列表

    for i in range(n_cases):  # 遍历每个用例编号
        is_violation = i % 2 == 0  # 奇偶交替：偶数编号为违规用例
        mmsi = f"9{str(i+1).zfill(8)}"  # 构建伪MMSI标识（9开头，8位数字）

        if is_violation:  # 如果是违规用例
            ship_data = {  # 违规船舶数据
                "ship": {
                    "mmsi": mmsi,  # MMSI标识
                    "name": f"TEST-VESSEL-{i:04d}",  # 船舶名称
                    "ship_type": "拖网渔船",  # 船舶类型
                },
                "current_position": {
                    "lat": 38.5 + rng.uniform(-0.1, 0.1),  # 纬度（随机偏移）
                    "lon": 122.0 + rng.uniform(-0.1, 0.1),  # 经度（随机偏移）
                    "speed": 1.2 + rng.uniform(0, 1.0),  # 航速（慢速，模拟捕捞）
                    "course": 45.0 + rng.uniform(-10, 10),  # 航向（随机偏移）
                    "timestamp": "2026-07-11T08:00:00Z",  # 时间戳
                    "status": "fishing",  # 船舶状态（捕捞中）
                },
                "history": [  # 历史轨迹数据
                    {
                        "lat": 38.5 + rng.uniform(-0.05, 0.05),  # 历史纬度
                        "lon": 122.0 + rng.uniform(-0.05, 0.05),  # 历史经度
                        "speed": 8.0 + rng.uniform(-1, 1),  # 历史航速
                        "course": 45.0,  # 历史航向
                        "timestamp": f"2026-07-11T07:{i%60:02d}:00Z",  # 历史时间戳
                    }
                    for _ in range(5)  # 5个历史点
                ],
                "context": {
                    "current_zone": "禁渔区A",  # 当前区域（禁渔区）
                    "time_info": "禁渔期",  # 时间信息（禁渔期）
                    "candidate": {
                        "type": "low_speed_fishing",  # 违规候选类型（低速捕捞）
                        "priority": "high",  # 优先级（高）
                    },
                },
            }
            ground_truth = {  # 地面真实标签
                "violation_detected": True,  # 检测到违规
                "violation_type": "illegal_fishing",  # 违规类型（非法捕捞）
                "violation_label": "非法捕捞",  # 违规标签（中文）
            }
        else:  # 如果是正常用例
            ship_data = {  # 正常船舶数据
                "ship": {
                    "mmsi": mmsi,  # MMSI标识
                    "name": f"TEST-VESSEL-{i:04d}",  # 船舶名称
                    "ship_type": "集装箱船",  # 船舶类型
                },
                "current_position": {
                    "lat": 38.5 + rng.uniform(-0.1, 0.1),  # 纬度（随机偏移）
                    "lon": 122.0 + rng.uniform(-0.1, 0.1),  # 经度（随机偏移）
                    "speed": 12.0 + rng.uniform(-2, 2),  # 航速（正常航速）
                    "course": 90.0 + rng.uniform(-5, 5),  # 航向（随机偏移）
                    "timestamp": "2026-07-11T08:00:00Z",  # 时间戳
                    "status": "underway",  # 船舶状态（航行中）
                },
                "history": [  # 历史轨迹数据
                    {
                        "lat": 38.5 + rng.uniform(-0.05, 0.05),  # 历史纬度
                        "lon": 122.0 + rng.uniform(-0.05, 0.05),  # 历史经度
                        "speed": 13.0 + rng.uniform(-1, 1),  # 历史航速
                        "course": 90.0,  # 历史航向
                        "timestamp": f"2026-07-11T07:{i%60:02d}:00Z",  # 历史时间戳
                    }
                    for _ in range(3)  # 3个历史点
                ],
                "context": {
                    "current_zone": "公海",  # 当前区域（公海，正常）
                    "time_info": "正常",  # 时间信息（正常）
                    "candidate": {"type": "normal", "priority": "low"},  # 无违规候选
                },
            }
            ground_truth = {  # 地面真实标签
                "violation_detected": False,  # 未检测到违规
                "violation_type": None,  # 违规类型为无
                "violation_label": None,  # 违规标签为无
            }

        test_set.append({
            "id": f"synthetic_{i:04d}",  # 用例唯一ID
            "ship_data": ship_data,  # 船舶数据
            "ground_truth": ground_truth,  # 地面真实标签
            "note": "违规" if is_violation else "正常",  # 用例标注说明
        })

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在
    with open(output_path, "w", encoding="utf-8") as f:  # 打开输出JSON文件
        json.dump(test_set, f, ensure_ascii=False, indent=2)  # 写入JSON（保留中文）

    n_violations = sum(1 for t in test_set if t["ground_truth"]["violation_detected"])  # 违规用例计数
    print(f"  [数据] 已生成合成测试集: {output_path}")
    print(f"        共 {n_cases} 条，其中违规 {n_violations} 条")

    return test_set  # 返回测试用例列表