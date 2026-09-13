/** 定位与 POI 检索封装：把 BMapGL 回调式接口转为 Promise，统一错误映射。
 *
 * 坐标说明：Geolocation 与 LocalSearch 返回的均为 BD09LL，与后端分析坐标系一致，
 * 不做任何本地坐标转换。SDK 由参数注入，便于离线单测。
 */

import type { Center } from '../types';
import type {
  BaiduMapApi, BMapAddressComponent, BMapGeolocation, BMapGeolocationResult,
  BMapLocalSearch, BMapLocalSearchResult, BMapPositionOptions,
} from '../map/baiduMapTypes';
import type { MapMode } from '../map/baiduMapConfig';

export type LocationErrorCode =
  | 'unsupported' | 'permission' | 'unavailable' | 'timeout' | 'invalid' | 'no-results' | 'failed';

export class LocationError extends Error {
  constructor(public code: LocationErrorCode, message: string) {
    super(message);
    this.name = 'LocationError';
  }
}

export type LocatedPosition = {
  center: Center;
  /** 定位精度半径（米）；SDK 未提供时为 null。 */
  accuracy: number | null;
  address: string | null;
};

export type PlaceResult = {
  id: string;
  title: string;
  address: string | null;
  center: Center;
};

export type SearchOptions = { radius?: number; limit?: number };

const LOCATE_TIMEOUT_MS = 12_000;
const SEARCH_TIMEOUT_MS = 12_000;
const STATUS_SUCCESS = 0;
const STATUS_PERMISSION_DENIED = 6;
const STATUS_TIMEOUT = 8;
/** 超过该精度（米）视为粗略定位，需要用户在地图上核对。 */
export const COARSE_ACCURACY_M = 200;
export const SEARCH_RADIUS_M = 5_000;
export const SEARCH_LIMIT = 10;

const round6 = (value: number) => Number(value.toFixed(6));

function toCenter(lng: unknown, lat: unknown): Center | null {
  if (typeof lng !== 'number' || typeof lat !== 'number') return null;
  if (!Number.isFinite(lng) || !Number.isFinite(lat)) return null;
  if (lng < -180 || lng > 180 || lat <= -85 || lat >= 85) return null;
  return { lng: round6(lng), lat: round6(lat) };
}

/** 拼接地址分量；直辖市等省与市同名时去掉连续重复段。 */
export function formatAddress(address?: BMapAddressComponent): string | null {
  if (!address || typeof address !== 'object') return null;
  const parts = [address.province, address.city, address.district, address.street, address.streetNumber]
    .filter((part): part is string => typeof part === 'string' && part.trim().length > 0)
    .map(part => part.trim());
  const unique = parts.filter((part, index) => index === 0 || part !== parts[index - 1]);
  return unique.length ? unique.join('') : null;
}

/** 地图模式对应的控件提示；real 返回 null 表示控件可用。 */
export function locationNotice(mode: MapMode): string | null {
  if (mode === 'real') return null;
  if (mode === 'loading') return '正在加载地图服务，稍后可使用定位与地点搜索。';
  return '配置浏览器地图密钥后，可使用获取当前位置与地点搜索；仍可手动输入坐标。';
}

function geolocationError(status: number): LocationError {
  if (status === STATUS_PERMISSION_DENIED) {
    return new LocationError('permission', '定位权限被拒绝，请在浏览器设置中允许定位后重试');
  }
  if (status === STATUS_TIMEOUT) {
    return new LocationError('timeout', '定位超时，请重试或手动输入坐标');
  }
  return new LocationError('unavailable', '当前环境无法获取位置，请搜索地点或手动输入坐标');
}

/** 浏览器定位；成功返回 BD09LL 坐标、精度与地址，失败按状态码给出可操作错误。 */
export function locate(api: BaiduMapApi, options?: BMapPositionOptions): Promise<LocatedPosition> {
  const Geolocation = api.Geolocation;
  if (!Geolocation) {
    return Promise.reject(new LocationError('unsupported', '当前地图脚本不支持浏览器定位，请搜索地点或手动输入坐标'));
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let instance: BMapGeolocation;
    const finish = (run: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      run();
    };
    const timer = setTimeout(
      () => finish(() => reject(new LocationError('timeout', '定位超时，请重试或手动输入坐标'))),
      LOCATE_TIMEOUT_MS
    );
    try {
      instance = new Geolocation();
      instance.getCurrentPosition(function (this: BMapGeolocation | undefined, result: BMapGeolocationResult | null) {
        const status = typeof this?.getStatus === 'function' ? this.getStatus() : instance.getStatus();
        if (status !== STATUS_SUCCESS) {
          finish(() => reject(geolocationError(status)));
          return;
        }
        const center = result?.point ? toCenter(result.point.lng, result.point.lat) : null;
        if (!center) {
          finish(() => reject(new LocationError('invalid', '定位返回的坐标无效，请重试或手动输入坐标')));
          return;
        }
        const rawAccuracy = result?.accuracy;
        const accuracy = typeof rawAccuracy === 'number' && Number.isFinite(rawAccuracy) && rawAccuracy >= 0
          ? Math.round(rawAccuracy) : null;
        finish(() => resolve({ center, accuracy, address: formatAddress(result?.address) }));
      }, options ?? { enableHighAccuracy: true, timeout: 10_000 });
    } catch {
      finish(() => reject(new LocationError('failed', '定位失败，请重试或手动输入坐标')));
    }
  });
}

function normalizeResults(results: BMapLocalSearchResult | null, limit: number): PlaceResult[] {
  if (!results || typeof results.getPoi !== 'function') return [];
  const count = typeof results.getCurrentNumPois === 'function'
    ? results.getCurrentNumPois()
    : typeof results.getNumPois === 'function' ? results.getNumPois() : 0;
  const items: PlaceResult[] = [];
  for (let index = 0; index < count && items.length < limit; index += 1) {
    try {
      const poi = results.getPoi(index);
      const center = poi?.point ? toCenter(poi.point.lng, poi.point.lat) : null;
      if (!poi || !center) continue;
      items.push({
        id: typeof poi.uid === 'string' && poi.uid ? poi.uid : `index-${index}`,
        title: typeof poi.title === 'string' && poi.title.trim() ? poi.title.trim() : '未命名地点',
        address: typeof poi.address === 'string' && poi.address.trim() ? poi.address.trim() : null,
        center,
      });
    } catch {
      // 单条结果结构异常时跳过，不影响其余结果。
    }
  }
  return items;
}

/** 以 center 为圆心的 POI 关键词检索；结果按距离排序并规范化为 BD09LL 六位小数。 */
export function searchPlaces(
  api: BaiduMapApi, keyword: string, center: Center, options: SearchOptions = {}
): Promise<PlaceResult[]> {
  const query = keyword.trim();
  if (!query) return Promise.reject(new LocationError('invalid', '请输入要搜索的地点关键词'));
  const LocalSearch = api.LocalSearch;
  if (!LocalSearch) {
    return Promise.reject(new LocationError('unsupported', '当前地图脚本不支持地点检索，请手动输入坐标'));
  }
  const radius = options.radius ?? SEARCH_RADIUS_M;
  const limit = options.limit ?? SEARCH_LIMIT;
  return new Promise((resolve, reject) => {
    let settled = false;
    let instance: BMapLocalSearch | undefined;
    const finish = (run: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      run();
    };
    const timer = setTimeout(
      () => finish(() => reject(new LocationError('timeout', '搜索服务无响应，请确认浏览器地图密钥已开通地点检索服务'))),
      SEARCH_TIMEOUT_MS
    );
    try {
      const point = new api.Point(center.lng, center.lat);
      instance = new LocalSearch(point, {
        pageCapacity: limit,
        onSearchComplete: results => {
          const items = normalizeResults(results, limit);
          if (!items.length) {
            finish(() => reject(new LocationError('no-results', '未找到相关地点，请尝试其他关键词')));
            return;
          }
          finish(() => resolve(items));
        },
      });
      instance.searchNearby(query, point, radius);
    } catch {
      finish(() => reject(new LocationError('failed', '地点搜索失败，请重试')));
    }
  });
}
