#!/bin/zsh
# 编译「今日.app 到 <脚本目录>/../build/今日.app。
# 用法：build.sh [项目根目录]　—　传了就写进 Info.plist 的 PILProjectRoot（App 用它起本地服务）
# 不会自动拷到 /Applications。
set -e
here=${0:A:h}
out=$here/../build
cache=$out/.modcache          # 沙盒里默认模块缓存目录不可写，显式指到 out/ 下
app="$out/今日.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$cache"

# 不指定 -target 时 swiftc 会按 SDK 版本定最低系统，可能比本机系统还新，导致打不开
swiftc -O -target arm64-apple-macos13.0 -module-cache-path "$out/.modcache" \
  -framework UserNotifications -framework ServiceManagement \
  -o "$app/Contents/MacOS/TodayPaper" "$here/main.swift"

cp "$here/Info.plist" "$app/Contents/Info.plist"
if [[ -n ${1:-} ]]; then
  /usr/libexec/PlistBuddy -c "Set :PILProjectRoot $1" "$app/Contents/Info.plist"
fi

iconset=$out/AppIcon.iconset
mkdir -p $iconset
# 图标：繁体单字 + 色线，脚本见同目录 make_char_icon.swift
swift -module-cache-path "$out/.modcache" "$here/make_char_icon.swift" 今 2B4A6F $out/icon-1024.png
for s in 16 32 128 256 512; do
  sips -z $s $s $out/icon-1024.png --out $iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) $out/icon-1024.png --out $iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns $iconset -o "$app/Contents/Resources/AppIcon.icns"

codesign --force --sign - "$app"
echo "built: $app"
