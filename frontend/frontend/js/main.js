(function() {
    'use strict';

    // ===================== 页面导航 =====================
    const navItems = document.querySelectorAll('.nav-item');
    const pages = document.querySelectorAll('.page');
    const pageTitle = document.getElementById('pageTitle');
    const pageNames = {
        dashboard: '仪表盘', monitor: '船舶监控', violation: '违规管理',
        disposal: '违规处置', query: '智能查询', knowledge: '知识库', settings: '系统设置'
    };

    navItems.forEach(item => {
        item.addEventListener('click', function(e) {
            e.preventDefault();
            const pageId = this.dataset.page;
            navItems.forEach(n => n.classList.remove('active'));
            this.classList.add('active');
            pages.forEach(p => p.classList.remove('active'));
            const targetPage = document.getElementById('page-' + pageId);
            if (targetPage) targetPage.classList.add('active');
            pageTitle.textContent = pageNames[pageId] || '仪表盘';
        });
    });

    // ===================== 时钟 =====================
    function updateTime() {
        const now = new Date();
        document.getElementById('currentTime').textContent =
            now.toLocaleString('zh-CN', { hour12: false });
    }
    updateTime();
    setInterval(updateTime, 1000);

    // ===================== 图表初始化 =====================
    function initCharts() {
        if (typeof echarts === 'undefined') return;

        // 船舶交通流量趋势
        const trafficChart = echarts.init(document.getElementById('chartTraffic'));
        const days = ['7/7', '7/8', '7/9', '7/10', '7/11', '7/12', '7/13'];
        trafficChart.setOption({
            tooltip: { trigger: 'axis' },
            legend: { data: ['进港', '出港', '在港'], bottom: 0, textStyle: { fontSize: 12 } },
            grid: { left: 40, right: 20, top: 10, bottom: 40 },
            xAxis: { type: 'category', data: days, axisLabel: { fontSize: 11 } },
            yAxis: { type: 'value', name: '艘次', nameTextStyle: { fontSize: 11 } },
            series: [
                {
                    name: '进港', type: 'bar', barWidth: 8, barGap: '20%',
                    itemStyle: { color: '#4285f4', borderRadius: [3,3,0,0] },
                    data: [86, 92, 78, 95, 88, 102, 76]
                },
                {
                    name: '出港', type: 'bar', barWidth: 8,
                    itemStyle: { color: '#0f9d58', borderRadius: [3,3,0,0] },
                    data: [72, 85, 68, 90, 82, 96, 70]
                },
                {
                    name: '在港', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
                    lineStyle: { color: '#f59e0b', width: 2 },
                    itemStyle: { color: '#f59e0b' },
                    areaStyle: { color: 'rgba(245,158,11,0.1)' },
                    data: [210, 225, 198, 240, 230, 265, 245]
                }
            ]
        });

        // 违规类型分布
        const violationChart = echarts.init(document.getElementById('chartViolationType'));
        violationChart.setOption({
            tooltip: { trigger: 'item', formatter: '{b}: {c}起 ({d}%)' },
            legend: { bottom: 0, textStyle: { fontSize: 12 } },
            series: [{
                type: 'pie', radius: ['40%', '70%'], center: ['50%', '45%'],
                avoidLabelOverlap: true,
                label: { show: true, formatter: '{b}', fontSize: 11 },
                emphasis: { label: { show: true, fontSize: 14 } },
                data: [
                    { value: 15, name: '超速', itemStyle: { color: '#ea4335' } },
                    { value: 10, name: '越界锚泊', itemStyle: { color: '#f59e0b' } },
                    { value: 8, name: '违规追越', itemStyle: { color: '#f97316' } },
                    { value: 7, name: '航线偏离', itemStyle: { color: '#4285f4' } },
                    { value: 4, name: '违规捕捞', itemStyle: { color: '#7c3aed' } },
                    { value: 3, name: '通信违规', itemStyle: { color: '#06b6d4' } }
                ]
            }]
        });

        // Resize charts on window resize
        window.addEventListener('resize', function() {
            trafficChart.resize();
            violationChart.resize();
        });
    }

    // ===================== 智能查询 =====================
    window.sendQuery = function() {
        const input = document.getElementById('chatInput');
        const query = input.value.trim();
        if (!query) return;
        sendQuickQuery(query);
        input.value = '';
    };

    window.sendQuickQuery = function(query) {
        const chatMessages = document.getElementById('chatMessages');
        const exampleQueryMsg = document.getElementById('exampleQueryMsg');
        const exampleResponseMsg = document.getElementById('exampleResponseMsg');
        const exampleQueryText = document.getElementById('exampleQueryText');
        const exampleResponseText = document.getElementById('exampleResponseText');

        exampleQueryMsg.style.display = 'flex';
        exampleQueryText.textContent = query;

        // 根据查询内容生成模拟回复
        let response = '';
        const q = query.toLowerCase();

        if (q.includes('轨迹') || (q.includes('闽海之星'))) {
            response = '<p><strong>🛳️ 船舶 "闽海之星" 最近航行轨迹查询结果</strong></p>' +
                '<p><strong>MMSI：</strong>412345678</p>' +
                '<p><strong>船舶类型：</strong>集装箱船</p>' +
                '<p><strong>当前状态：</strong>🚢 航行中</p>' +
                '<p><strong>当前位置：</strong>26°05\'12"N, 119°38\'45"E（福州港主航道）</p>' +
                '<p><strong>航速：</strong>12.5 kn</p>' +
                '<p><strong>航向：</strong>045°</p>' +
                '<hr style="margin:8px 0;border-color:var(--border)">' +
                '<p><strong>最近6小时轨迹：</strong></p>' +
                '<p>07:30 出发港 → 09:15 #1浮标 → 10:40 主航道入口 → 12:20 #3浮标 → 14:00 当前</p>';
        } else if (q.includes('限速') || q.includes('航道')) {
            response = '<p><strong>📋 福州港主航道限速规定查询结果</strong></p>' +
                '<p>根据《福州港港口章程》第三章第12条规定：</p>' +
                '<ul>' +
                '<li><strong>主航道（#1~#8浮标）：</strong>限速 ≤ 12 kn</li>' +
                '<li><strong>港池水域：</strong>限速 ≤ 8 kn</li>' +
                '<li><strong>靠离泊作业：</strong>限速 ≤ 4 kn</li>' +
                '<li><strong>能见度＜1nm时：</strong>限速 ≤ 8 kn</li>' +
                '</ul>' +
                '<p>📌 当前查询船舶"闽海之星"航速12.5kn，已超出主航道限速标准！</p>';
        } else if (q.includes('统计') || q.includes('违规') && (q.includes('今日') || q.includes('辖区'))) {
            response = '<p><strong>📊 今日辖区违规事件统计报告</strong></p>' +
                '<table style="width:100%;border-collapse:collapse;font-size:13px;">' +
                '<tr style="background:#f0f4f8;"><th style="padding:6px 8px;text-align:left;">违规类型</th><th style="padding:6px 8px;">数量</th><th style="padding:6px 8px;">占比</th></tr>' +
                '<tr><td style="padding:4px 8px;">🚀 超速</td><td style="padding:4px 8px;text-align:center;">15</td><td style="padding:4px 8px;text-align:center;">31.9%</td></tr>' +
                '<tr><td style="padding:4px 8px;">⚓ 越界锚泊</td><td style="padding:4px 8px;text-align:center;">10</td><td style="padding:4px 8px;text-align:center;">21.3%</td></tr>' +
                '<tr><td style="padding:4px 8px;">🚢 违规追越</td><td style="padding:4px 8px;text-align:center;">8</td><td style="padding:4px 8px;text-align:center;">17.0%</td></tr>' +
                '<tr><td style="padding:4px 8px;">🧭 航线偏离</td><td style="padding:4px 8px;text-align:center;">7</td><td style="padding:4px 8px;text-align:center;">14.9%</td></tr>' +
                '<tr><td style="padding:4px 8px;">🎣 违规捕捞</td><td style="padding:4px 8px;text-align:center;">4</td><td style="padding:4px 8px;text-align:center;">8.5%</td></tr>' +
                '<tr><td style="padding:4px 8px;">📡 通信违规</td><td style="padding:4px 8px;text-align:center;">3</td><td style="padding:4px 8px;text-align:center;">6.4%</td></tr>' +
                '</table>' +
                '<p style="margin-top:8px;"><strong>合计：</strong>47起（已处置43起，处置率91.5%）</p>';
        } else if (q.includes('锚泊')) {
            response = '<p><strong>⚓ 船舶港区锚泊规定查询结果</strong></p>' +
                '<p>根据相关法规，船舶在港区锚泊应当遵守以下规定：</p>' +
                '<ol>' +
                '<li>必须在指定的锚泊区域内锚泊，禁止在航道、调头区等水域锚泊</li>' +
                '<li>锚泊时应保持AIS设备持续开启，显示锚泊状态</li>' +
                '<li>锚泊期间应按规定显示锚泊信号灯</li>' +
                '<li>遇恶劣天气时应加强值班，做好走锚应急准备</li>' +
                '<li>福州港指定锚泊区：#1锚地（26°10\'N, 119°35\'E）、#2锚地（26°15\'N, 119°40\'E）</li>' +
                '</ol>';
        } else if (q.includes('超速') || q.includes('违规')) {
            response = '<p><strong>⚠️ 船舶超速违规分析</strong></p>' +
                '<p>当前监测到"闽海之星"在福州港主航道#3浮标附近航速达18.7kn，超过该航段限速12kn标准55.8%。</p>' +
                '<p><strong>风险分析：</strong></p>' +
                '<ul>' +
                '<li>该航段通航密度大，超速航行显著增加碰撞风险</li>' +
                '<li>附近有#3浮标，高速航行时避让操作空间不足</li>' +
                '<li>建议立即通过VHF CH16发出警告</li>' +
                '</ul>' +
                '<p><strong>处置建议：</strong>依据《海上交通安全法》第103条，建议罚款5000-20000元。</p>';
        } else {
            response = '<p>📌 已收到您的问题：<strong>"' + query + '"</strong></p>' +
                '<p>正在查询中...请稍候。</p>' +
                '<p>💡 您也可以尝试以下查询：</p>' +
                '<ul>' +
                '<li>查询某艘船舶轨迹（如"查询闽海之星轨迹"）</li>' +
                '<li>了解航道规定（如"主航道限速"）</li>' +
                '<li>查看违规统计数据（如"今日违规统计"）</li>' +
                '</ul>';
        }

        exampleResponseMsg.style.display = 'flex';
        exampleResponseText.innerHTML = response;

        // 滚动到底部
        chatMessages.scrollTop = chatMessages.scrollHeight;
    };

    // ===================== 初始化 =====================
    document.addEventListener('DOMContentLoaded', function() {
        initCharts();
        // 模拟实时告警闪烁
        setInterval(function() {
            const rows = document.querySelectorAll('.alert-row.alert-danger');
            rows.forEach(row => {
                row.style.transition = 'background 1s';
                row.style.background = row.style.background === 'rgba(234,67,53,0.05)' ? '' : 'rgba(234,67,53,0.05)';
            });
        }, 2000);
    });

})();
