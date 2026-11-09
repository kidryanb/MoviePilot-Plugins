"""SyncHandler 的目标网盘作用域（M1「同盘优先」）。

上游 SyncHandler 在初始化时把「当前转存网盘」及其能力服务、转存路径、媒体库根目录
存成实例属性，全部转存、整理、后处理代码都直接读这些属性。为了不改动上百处调用，
这里把这些属性换成按作用域取值的描述符：

- 默认（没有进入作用域）时返回初始化时写入的值，行为与上游一致；
- 在 ``drive_scope("quark")`` 内，返回为夸克预先算好的一套值。

作用域用 ``contextvars`` 记录，只影响当前线程（以及显式用 ``copy_context`` 派生的
工作线程），并发处理的其他订阅不受影响。
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

from ...core.cloud import CloudDriveCapability
from ...core.drive_routing import route_share

#: 当前线程选定的目标网盘标识；None 表示使用默认网盘
_ACTIVE_DRIVE: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "cloudsubscribe_active_drive", default=None
)

#: 为 True 时不按分享类型切换网盘（洗版等需要固定在默认盘的流程）
_ROUTING_DISABLED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "cloudsubscribe_drive_routing_disabled", default=False
)

_UNSET = object()

#: 与目标网盘绑定的能力服务属性
CAPABILITY_ATTRS = {
    "_cloud_auth": CloudDriveCapability.AUTHENTICATION,
    "_cloud_account": CloudDriveCapability.ACCOUNT,
    "_share_transfer": CloudDriveCapability.SHARE_TRANSFER,
    "_offline_download": CloudDriveCapability.OFFLINE_DOWNLOAD,
    "_cloud_directories": CloudDriveCapability.DIRECTORY_READ,
    "_cloud_query": CloudDriveCapability.FILE_QUERY,
    "_cloud_mutations": CloudDriveCapability.FILE_MUTATION,
    "_checksum_rename": CloudDriveCapability.CHECKSUM_RENAME,
    "_cloud_batch_mutations": CloudDriveCapability.BATCH_FILE_MUTATION,
    "_playback_reference": CloudDriveCapability.PLAYBACK_REFERENCE,
    "_offline_tasks": CloudDriveCapability.OFFLINE_TASKS,
    "_cloud_upload": CloudDriveCapability.LOCAL_UPLOAD,
}

#: 全部按作用域取值的属性；其中 _CLOUD_MEDIA_ROOT 在上游是类属性，默认 "/"
SCOPED_ATTRS = (
    "_cloud_drive",
    "_cloud_transfer_path",
    "_CLOUD_MEDIA_ROOT",
    "_batch_size",
    "_strm_generate_enabled",
    "_strm_generator",
    *CAPABILITY_ATTRS,
)


class DriveScopedAttribute:
    """按当前作用域返回对应网盘取值的数据描述符。"""

    def __init__(self, name: str, default: Any = _UNSET):
        self.name = name
        self.storage = f"_drive_default{name}"
        self.default = default

    def __get__(self, obj: Any, owner: Any = None) -> Any:
        if obj is None:
            return self if self.default is _UNSET else self.default
        key = _ACTIVE_DRIVE.get()
        if key:
            profile = obj.__dict__.get("_drive_profiles", {}).get(key)
            if profile is not None and self.name in profile:
                return profile[self.name]
        value = obj.__dict__.get(self.storage, self.default)
        if value is _UNSET:
            raise AttributeError(self.name)
        return value

    def __set__(self, obj: Any, value: Any) -> None:
        obj.__dict__[self.storage] = value


def install_scoped_attributes(cls: type) -> type:
    """把 SCOPED_ATTRS 安装为类上的描述符，保留原有类属性作为默认值。"""
    for name in SCOPED_ATTRS:
        default = cls.__dict__.get(name, _UNSET)
        setattr(cls, name, DriveScopedAttribute(name, default))
    return cls


def current_drive_key() -> Optional[str]:
    """返回当前线程选定的目标网盘标识。"""
    return _ACTIVE_DRIVE.get()


def run_in_current_scope(func: Callable, *args: Any, **kwargs: Any) -> Callable[[], Any]:
    """
    生成在当前作用域里执行的可调用对象，供线程池 / 新线程使用。

    :return: 无参可调用对象
    """
    context = contextvars.copy_context()
    return lambda: context.run(func, *args, **kwargs)


class DriveScopeMixin:
    """SyncHandler 的作用域入口与目标盘配置构建。"""

    def _build_drive_profiles(self, target_keys, configured_batch_size: int) -> None:
        """
        为每个同盘目标预先算好一套网盘相关属性。

        :param target_keys: 默认盘之外的同盘目标标识
        :param configured_batch_size: 用户配置的转存批大小
        """
        profiles: Dict[str, Dict[str, Any]] = {}
        registry = getattr(self, "_cloud_drive_registry", None)
        default_key = getattr(self.__dict__.get("_drive_default_cloud_drive"), "key", "")
        for key in target_keys or []:
            if not registry or key == default_key:
                continue
            try:
                provider = registry.get(key)
            except KeyError:
                continue
            profile: Dict[str, Any] = {"_cloud_drive": provider}
            for attr, capability in CAPABILITY_ATTRS.items():
                profile[attr] = (
                    provider.require(capability) if provider.supports(capability) else None
                )
            transfer_path = (self._cloud_transfer_paths or {}).get(key) or "/"
            profile["_cloud_transfer_path"] = str(transfer_path).strip().rstrip("/") or "/"
            profile["_CLOUD_MEDIA_ROOT"] = self._normalize_cloud_path(
                (self._cloud_media_paths or {}).get(key) or "/"
            )
            policy = getattr(provider, "policy", None)
            profile["_batch_size"] = min(
                configured_batch_size,
                policy.max_batch_size if policy and policy.supports_batch else configured_batch_size,
            )
            # 夸克 STRM/302 属于 M3；在那之前其他网盘不生成 STRM。
            profile["_strm_generate_enabled"] = False
            profile["_strm_generator"] = None
            profiles[key] = profile
        self.__dict__["_drive_profiles"] = profiles

    def scoped_drive_key(self) -> Optional[str]:
        """当前作用域里生效的非默认网盘标识；默认盘返回 None。"""
        key = _ACTIVE_DRIVE.get()
        return key if key and key in self.__dict__.get("_drive_profiles", {}) else None

    def same_cloud_drive_keys(self) -> list:
        """返回已就绪的同盘目标（不含默认盘）。"""
        return list(self.__dict__.get("_drive_profiles", {}))

    @contextmanager
    def drive_scope(self, key: Optional[str]) -> Iterator[Optional[str]]:
        """
        在作用域内把转存目标切到指定网盘；不是同盘目标时保持默认盘。

        :param key: 目标网盘标识
        :return: 实际生效的网盘标识（None 表示默认盘）
        """
        normalized = str(key or "").strip().lower()
        if not normalized or normalized not in self.__dict__.get("_drive_profiles", {}):
            # 显式回到默认盘：嵌套在其他网盘作用域里时也要能切回来
            token = _ACTIVE_DRIVE.set(None) if _ACTIVE_DRIVE.get() else None
            try:
                yield None
            finally:
                if token is not None:
                    _ACTIVE_DRIVE.reset(token)
            return
        token = _ACTIVE_DRIVE.set(normalized)
        try:
            yield normalized
        finally:
            _ACTIVE_DRIVE.reset(token)

    def route_drive_for_url(self, share_url: str) -> Optional[str]:
        """
        按分享所属网盘决定目标盘；返回 None 表示使用默认盘。

        :param share_url: 分享链接
        """
        profiles = self.__dict__.get("_drive_profiles", {})
        if not profiles or not share_url or _ROUTING_DISABLED.get():
            return None
        try:
            source = self._resource_provider_for_url(str(share_url).strip())
        except Exception:
            return None
        default_drive = self.__dict__.get("_drive_default_cloud_drive")
        return route_share(
            getattr(source, "key", None),
            getattr(default_drive, "key", None),
            list(profiles),
        )

    @contextmanager
    def no_drive_routing(self) -> Iterator[None]:
        """在作用域内固定使用默认网盘，不按分享类型切换。"""
        token = _ROUTING_DISABLED.set(True)
        try:
            with self.drive_scope(None):
                yield
        finally:
            _ROUTING_DISABLED.reset(token)

    @contextmanager
    def drive_scope_for_url(self, share_url: str) -> Iterator[Optional[str]]:
        """按分享链接进入对应网盘的作用域。"""
        with self.drive_scope(self.route_drive_for_url(share_url)) as key:
            yield key
