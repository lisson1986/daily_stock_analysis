"""Source allowlist, dated evidence and raw daily bars for the paper adapter."""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse

from src.research.paper import eligible, validate_bar


def allow_url(url, domains):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower().rstrip('.')
    return parsed.scheme == 'https' and not parsed.username and any(
        host == d or host.endswith('.' + d) for d in domains)


def accepted_evidence(response, cfg, now):
    items, rejected = [], []
    for result in response.results:
        if not allow_url(result.url, cfg['source_domains']):
            rejected.append({'url': result.url, 'reason': 'domain_not_allowed'})
            continue
        published = None
        try:
            published = datetime.fromisoformat((result.published_date or '').replace('Z', '+00:00'))
            if published.tzinfo is None:
                published = published.replace(tzinfo=now.tzinfo)
        except ValueError:
            pass
        fresh = published is not None and timedelta(0) <= now - published <= timedelta(days=cfg['news_days'])
        items.append({'title': result.title, 'snippet': result.snippet, 'url': result.url,
                      'published_at': result.published_date, 'retrieved_at': now.isoformat(),
                      'date_verified': published is not None, 'fresh': fresh,
                      'provider': response.provider,
                      'content_hash': hashlib.sha256((result.title + result.snippet).encode()).hexdigest()})
    return {'items': items, 'rejected': rejected, 'available': response.success,
            'error': response.error_message}


def calendar_sessions(start, end):
    # Existing is_market_open() is fail-open; paper execution must be fail-closed.
    import exchange_calendars as xcals
    cal = xcals.get_calendar('XSHG')
    return [s.date().isoformat() for s in cal.sessions_in_range(start, end)]


def raw_history(code, start, end):
    """Unadjusted bars only. No fallback to DSA's default adjusted history."""
    from src.services.screening.source_guard import call_with_timeout
    errors = []
    try:
        return _tencent_raw_history(code, start, end)
    except Exception as exc:
        errors.append(f'tencent_raw: {exc}')
    try:
        import akshare as ak
        import pandas as pd
        frame = call_with_timeout(lambda: ak.stock_zh_a_hist(
            symbol=code, period='daily', start_date=start.replace('-', ''),
            end_date=end.replace('-', ''), adjust=''), timeout_sec=15, label='paper_raw_akshare')
        if frame is None or frame.empty:
            raise ValueError('Empty unadjusted history')
        frame = frame.rename(columns={'日期': 'date', '开盘': 'open', '收盘': 'close',
                                      '最高': 'high', '最低': 'low', '成交量': 'volume'})
        frame['date'] = frame['date'].astype(str).str[:10]
        for col in ('open', 'close', 'high', 'low', 'volume'):
            frame[col] = pd.to_numeric(frame[col], errors='raise')
        frame['volume'] *= 100  # AkShare stock_zh_a_hist volume is in lots.
        frame['adjustment'] = 'none'
        frame['source'] = 'akshare_raw_day'
        frame = frame[(frame['date'] >= start) & (frame['date'] <= end)]
        if frame.empty or frame['date'].duplicated().any():
            raise ValueError('Empty or duplicate unadjusted bars')
        return frame.sort_values('date').reset_index(drop=True)
    except Exception as exc:
        errors.append(f'akshare_raw: {exc}')
    raise ValueError('Unadjusted history sources failed: ' + '; '.join(errors))


def _tencent_raw_history(code, start, end):
    import pandas as pd
    import requests
    from data_provider.tencent_fetcher import _lots_to_shares, _to_tencent_symbol
    symbol = _to_tencent_symbol(code)
    if not symbol or not eligible(code):
        raise ValueError('Unsupported execution symbol')
    response = requests.get('https://web.ifzq.gtimg.cn/appstock/app/kline/mkline',
                            params={'param': f'{symbol},day,{start},{end},320'}, timeout=12)
    response.raise_for_status()
    payload = response.json()
    item = payload.get('data', {}).get(symbol, {})
    rows = item.get('day')
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'No unadjusted execution history for {code}')
    data = []
    for row in rows:
        if len(row) < 6 or not start <= str(row[0]) <= end:
            continue
        data.append({'date': str(row[0]), 'open': float(row[1]), 'close': float(row[2]),
                     'high': float(row[3]), 'low': float(row[4]),
                     'volume': float(_lots_to_shares(row[5], symbol)), 'adjustment': 'none',
                     'source': 'tencent_raw_day'})
    df = pd.DataFrame(data)
    if df.empty or df['date'].duplicated().any():
        raise ValueError('Empty or duplicate raw daily bars')
    return df.sort_values('date').reset_index(drop=True)


def price_tick(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def execution_bar(code, history, session):
    """Conservative bounds. Exclude ST/new listings and adjusted-price discontinuities.

    No buy at a daily upper-limit touch and no sell at a lower-limit touch.
    The deliberately stricter rule avoids inventing queue fills from daily bars.
    """
    rows = history[history['date'] <= session]
    if rows.empty or len(rows) < 20:
        raise ValueError(f'Missing current session or insufficient history: {code}')
    if rows.iloc[-1]['date'] != session:
        return suspension_bar(code, rows, session)
    bar = rows.iloc[-1].to_dict()
    prev = float(rows.iloc[-2]['close'])
    # ST, listing date and exchange reference prices are verified by live adapter below.
    limit_pct = 0.20 if code.startswith(('300', '301')) else 0.10
    upper, lower = price_tick(prev * (1 + limit_pct)), price_tick(prev * (1 - limit_pct))
    bar['buyable'] = bar['volume'] > 0 and bar['high'] < upper - 0.005
    bar['sellable'] = bar['volume'] > 0 and bar['low'] > lower + 0.005
    bar.setdefault('source', 'raw_daily_fixture')
    validate_bar(bar, session)
    return bar


def suspension_bar(code, history, session):
    """Carry a stale valuation only after a dated official suspension-list match."""
    import akshare as ak
    from src.services.screening.source_guard import call_with_timeout
    frame = call_with_timeout(lambda: ak.stock_tfp_em(date=session.replace('-', '')),
                              timeout_sec=12, label='paper_suspension_list')
    if frame is None or '代码' not in frame.columns:
        raise ValueError('Suspension status unavailable; no inferred zero-volume bars')
    matches = frame[frame['代码'].astype(str).str.zfill(6) == code]
    if matches.empty:
        raise ValueError(f'Missing current daily bar without confirmed suspension: {code}')
    price = float(history.iloc[-1]['close'])
    bar = {'date': session, 'open': price, 'high': price, 'low': price, 'close': price, 'volume': 0,
           'adjustment': 'none', 'buyable': False, 'sellable': False, 'suspended': True,
           'price_date': str(history.iloc[-1]['date']), 'valuation_stale': True,
           'source': 'confirmed_suspension_previous_close',
           'suspension_evidence': matches.astype(str).to_dict('records')}
    validate_bar(bar, session)
    return bar


def select_pool(snapshot, cfg):
    """Use the bundled rule scorer; no domestic research/news enrichment."""
    import pandas as pd
    from src.services.screening.models import ScreeningConfig
    from src.services.screening.scorer import compute_screen_scores
    df = snapshot.copy()
    df['code'] = df['code'].astype(str).str.zfill(6)
    for col in ('price', 'amount', 'change_pct'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
    safe = df['code'].map(eligible) & ~df['name'].astype(str).str.contains(r'ST|退', case=False)
    df = df[safe & (df['price'] > 0) & (df['amount'] >= 100000000)]
    limits = df['code'].str.startswith(('300', '301')).map({True: 19.5, False: 9.5})
    df = df[(df['change_pct'] < limits) & (df['change_pct'] > -5)]
    df = compute_screen_scores(df, ScreeningConfig()).sort_values(['screen_score', 'amount'], ascending=False)
    watch = snapshot[snapshot['code'].astype(str).str.zfill(6).isin(cfg['watchlist'])].copy()
    watch['code'] = watch['code'].astype(str).str.zfill(6)
    watch['screen_score'] = None
    watch['screen_score'] = float('nan')
    pool = pd.concat([watch, df]).drop_duplicates('code').head(cfg['research_count'])
    clean = []
    for row in pool.to_dict('records'):
        clean.append({k: v if not isinstance(v, float) or math.isfinite(v) else None for k, v in row.items()})
    return clean


def verify_security(code, session):
    """Check listing age, ST status and corporate events before assuming price bands."""
    import akshare as ak
    from src.services.screening.source_guard import call_with_timeout
    info = call_with_timeout(lambda: ak.stock_individual_info_em(symbol=code),
                             timeout_sec=12, label='paper_security_info')
    if info is None or info.empty:
        raise ValueError('Security metadata unavailable')
    values = dict(zip(info['item'].astype(str), info['value']))
    name = str(values.get('股票简称', ''))
    listed = str(values.get('上市时间', ''))[:8]
    if not name or 'ST' in name.upper() or '退' in name or len(listed) != 8:
        raise ValueError('ST/delisted/unknown security metadata')
    listing_date = datetime.strptime(listed, '%Y%m%d').date()
    if date.fromisoformat(session) - listing_date < timedelta(days=60):
        raise ValueError('New listing excluded from simulation')
    # AkShare dividend detail identifies dated ex-right/ex-dividend events.
    events = call_with_timeout(
        lambda: ak.stock_history_dividend_detail(symbol=code, indicator='分红'),
        timeout_sec=12, label='paper_dividends')
    if events is None or '除权除息日' not in events.columns:
        raise ValueError('Corporate action calendar unavailable')
    dates = events['除权除息日'].astype(str).str[:10]
    if (dates == session).any():
        raise ValueError('Ex-dividend day: reconcile account before continuing')
    # Rights issues also change exchange reference prices/entitlements.
    rights = call_with_timeout(
        lambda: ak.stock_history_dividend_detail(symbol=code, indicator='配股'),
        timeout_sec=12, label='paper_rights')
    if rights is None:
        raise ValueError('Rights issue calendar unavailable')
    for column in ('除权日', '配股上市日'):
        if column in rights and (rights[column].astype(str).str[:10] == session).any():
            raise ValueError('Rights issue requires account reconciliation')
    return {'name': name, 'listing_date': listing_date.isoformat()}
