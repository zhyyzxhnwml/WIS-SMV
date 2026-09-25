"""
故障注入器 —— 通过 contextmanager 在实验前后注入/恢复故障
===========================================================
"""
from __future__ import annotations

import subprocess
import time
import os
import csv
import tempfile
from pathlib import Path
from contextlib import contextmanager


@contextmanager
def without_ollama():
    """
    故障注入：关闭 Ollama。

    进入上下文时杀掉 ollama.exe 进程，
    退出上下文时重启 ollama serve。

    使用示例:
        with without_ollama():
            # 在这个上下文中，Ollama 不可用
            result = subprocess.run(["python", "run.py", ...])
    """
    print("[故障注入] 正在关闭 Ollama...")
    _kill_process("ollama.exe")  # 杀掉ollama进程
    time.sleep(1)  # 等待1秒确保进程已终止

    try:
        yield  # 进入上下文，执行with块中的代码
    finally:
        print("[故障注入] 正在重启 Ollama...")
        subprocess.Popen(  # 启动ollama服务进程
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,  # 丢弃标准输出
            stderr=subprocess.DEVNULL,  # 丢弃标准错误
        )
        time.sleep(3)  # 等待3秒确保服务启动完成
        print("[故障注入] Ollama 已重启")


@contextmanager
def without_ml_model():
    """
    故障注入：删除 ML 模型文件（triage_model.txt）。

    让 Cascade 系统进入"自动训练初始模型"的降级路径。
    """
    model_path = Path("triage_model.txt")  # 原始ML模型文件路径
    backup_path = Path("triage_model.txt.bak")  # 备份文件路径

    if model_path.exists():
        print("[故障注入] 正在备份并删除 ML 模型文件...")
        model_path.replace(backup_path)  # 将模型文件重命名为备份文件
    else:
        print("[故障注入] ML 模型文件不存在，跳过")

    try:
        yield  # 进入上下文，执行with块中的代码
    finally:
        if backup_path.exists():
            print("[故障注入] 恢复 ML 模型文件...")
            backup_path.replace(model_path)  # 将备份文件恢复为模型文件


@contextmanager
def without_milvus():
    """
    故障注入：禁用 Milvus 数据库。

    通过重命名 Milvus 目录使其不可访问。
    """
    milvus_dir = Path("milvus_lite.db")  # 原始Milvus数据库目录路径
    backup_dir = Path("milvus_lite.db.bak")  # 备份目录路径

    if milvus_dir.exists():
        print("[故障注入] 正在禁用 Milvus 数据库...")
        milvus_dir.replace(backup_dir)  # 将Milvus目录重命名为备份目录
    else:
        print("[故障注入] Milvus 目录不存在，跳过")

    try:
        yield  # 进入上下文，执行with块中的代码
    finally:
        if backup_dir.exists():
            print("[故障注入] 恢复 Milvus 数据库...")
            backup_dir.replace(milvus_dir)  # 将备份目录恢复为Milvus目录


@contextmanager
def with_all_faults():
    """
    组合故障：LLM + ML 模型 + Milvus 全部不可用。

    测试 Cascade 在"最坏情况"下的降级能力：
    - L1 加速器应能正常运行（纯规则引擎）
    - L2 ML 路由应降级为"无模型"模式
    - L3 LLM 应进入 Mock 模式
    """
    print("[故障注入] 注入全部故障: LLM + ML 模型 + Milvus")
    # 先杀 Ollama
    _kill_process("ollama.exe")  # 杀掉ollama进程

    # 备份并删除 ML 模型
    model_path = Path("triage_model.txt")  # 原始ML模型文件路径
    model_backup = Path("triage_model.txt.bak")  # 模型备份文件路径
    if model_path.exists():
        model_path.replace(model_backup)  # 将模型文件重命名为备份文件

    # 备份并删除 Milvus
    milvus_dir = Path("milvus_lite.db")  # 原始Milvus数据库目录路径
    milvus_backup = Path("milvus_lite.db.bak")  # Milvus备份目录路径
    if milvus_dir.exists():
        milvus_dir.replace(milvus_backup)  # 将Milvus目录重命名为备份目录

    time.sleep(1)  # 等待1秒确保所有故障注入完成

    try:
        yield  # 进入上下文，执行with块中的代码
    finally:
        print("[故障注入] 恢复全部故障...")
        if model_backup.exists():
            model_backup.replace(model_path)  # 恢复ML模型文件
        if milvus_backup.exists():
            milvus_backup.replace(milvus_dir)  # 恢复Milvus数据库目录

        subprocess.Popen(  # 重启ollama服务
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,  # 丢弃标准输出
            stderr=subprocess.DEVNULL,  # 丢弃标准错误
        )
        time.sleep(3)  # 等待3秒确保服务启动完成
        print("[故障注入] 全部已恢复")


@contextmanager
def with_temporary_csv(original_csv: str, fault_type: str):  # original_csv: 原始CSV文件路径; fault_type: 要注入的故障类型名称
    """
    创建一个临时的脏数据 CSV，实验结束后自动删除。

    参数:
        original_csv: 原始 CSV 路径
        fault_type: 异常类型，同 data_prep.create_dirty_csv

    使用示例:
        with with_temporary_csv("data.csv", "extreme_values") as dirty_csv:
            result = subprocess.run(["python", "run.py", "--input", dirty_csv, ...])
    """
    from ..data_prep import create_dirty_csv  # 导入脏数据创建函数

    tmp = tempfile.NamedTemporaryFile(  # 创建临时文件对象
        mode="w", suffix=".csv", delete=False, encoding="utf-8"  # 写入模式，CSV后缀，不自动删除，UTF-8编码
    )
    tmp.close()  # 关闭临时文件（保留文件本身）

    try:
        create_dirty_csv(original_csv, fault_type, tmp.name)  # 基于原始CSV创建带故障的脏数据CSV
        yield tmp.name  # 返回临时CSV文件路径
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)  # 删除临时CSV文件


def _kill_process(name: str):  # name: 要杀掉的进程名称（如"ollama.exe"）
    """杀掉指定名称的进程（Windows 下用 taskkill，Linux 用 pkill）"""
    if os.name == "nt":  # Windows系统
        subprocess.run(
            ["taskkill", "/f", "/im", name],  # 强制终止指定名称的进程
            capture_output=True, text=True,  # 捕获输出，以文本形式返回
        )
    else:  # Linux/Unix系统
        subprocess.run(
            ["pkill", "-9", name.replace(".exe", "")],  # 发送SIGKILL信号终止进程
            capture_output=True, text=True,  # 捕获输出，以文本形式返回
        )