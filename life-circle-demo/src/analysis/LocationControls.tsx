/** 分析中心便捷输入：获取当前位置与 POI 搜索；选中结果复用既有选点链路，不自动触发分析。 */

import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Input } from 'antd';
import { AimOutlined, SearchOutlined } from '@ant-design/icons';
import type { Center } from '../types';
import { useBaiduMap } from '../map/useBaiduMap';
import {
  COARSE_ACCURACY_M, locate, LocationError, locationNotice, searchPlaces,
  type LocatedPosition, type PlaceResult,
} from './location';
import './location.css';

type Props = { center: Center; onPick: (center: Center) => void };

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof LocationError ? error.message : fallback;
}

export function LocationControls({ center, onPick }: Props) {
  const { api, mode } = useBaiduMap();
  const [locating, setLocating] = useState(false);
  const [located, setLocated] = useState<LocatedPosition | null>(null);
  const [locateError, setLocateError] = useState<string | null>(null);
  const [keyword, setKeyword] = useState('');
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<PlaceResult[] | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchStatus, setSearchStatus] = useState<string | null>(null);
  const revision = useRef(0);
  useEffect(() => () => { revision.current += 1; }, []);

  async function doLocate() {
    if (!api || locating) return;
    const current = ++revision.current;
    setLocating(true); setLocateError(null); setLocated(null);
    try {
      const position = await locate(api);
      if (revision.current !== current) return;
      setLocated(position);
      onPick(position.center);
    } catch (error) {
      if (revision.current !== current) return;
      setLocateError(errorMessage(error, '定位失败，请重试或手动输入坐标'));
    } finally {
      if (revision.current === current) setLocating(false);
    }
  }

  async function doSearch() {
    if (!api || searching) return;
    const current = ++revision.current;
    setSearching(true); setSearchError(null); setResults(null); setSearchStatus(null);
    try {
      const items = await searchPlaces(api, keyword, center);
      if (revision.current !== current) return;
      setResults(items);
    } catch (error) {
      if (revision.current !== current) return;
      setSearchError(errorMessage(error, '地点搜索失败，请重试'));
    } finally {
      if (revision.current === current) setSearching(false);
    }
  }

  function select(item: PlaceResult) {
    onPick(item.center);
    setResults(null);
    setSearchError(null);
    setSearchStatus(`已选择：${item.title}${item.address ? ` · ${item.address}` : ''}`);
  }

  if (mode !== 'real' || !api) return <p className="api-muted">{locationNotice(mode)}</p>;
  const coarse = located !== null && (located.accuracy === null || located.accuracy > COARSE_ACCURACY_M);
  return <div className="api-location">
    <Button aria-label="获取当前位置" icon={<AimOutlined />} onClick={() => void doLocate()} loading={locating}>获取当前位置</Button>
    {located && <p className={`api-location-status${coarse ? ' warn' : ''}`}>
      已定位：{located.address || '地址未知'} · 定位精度约 {located.accuracy === null ? '未知' : `${located.accuracy} 米`}
      {coarse ? '，定位可能偏差较大，请在地图上核对' : ''}
    </p>}
    {locateError && <Alert type="error" title={locateError} showIcon />}
    <div className="api-location-row">
      <Input
        aria-label="搜索地点"
        placeholder="搜索地点，如：人民公园、中关村"
        value={keyword}
        allowClear
        onChange={event => setKeyword(event.target.value)}
        onPressEnter={() => void doSearch()}
      />
      <Button aria-label="搜索" icon={<SearchOutlined />} onClick={() => void doSearch()} loading={searching}>搜索</Button>
    </div>
    {searchError && <Alert type="error" title={searchError} showIcon />}
    {results && <ul className="api-search-results" aria-label="搜索结果">
      {results.map(item => <li key={item.id}>
        <button type="button" onClick={() => select(item)}>
          <span className="api-poi-title">{item.title}</span>
          {item.address && <span className="api-poi-address">{item.address}</span>}
        </button>
      </li>)}
    </ul>}
    {searchStatus && <p className="api-location-status">{searchStatus}</p>}
  </div>;
}
