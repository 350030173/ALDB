
@echo off
::	【有道云笔记】github上传项目步骤
::	https://share.note.youdao.com/s/AzNuXRAH

::	查看代理：git config --global --list | grep proxy
::	设置代理：git config --global http.proxy http://127.0.0.1:7897
::	设置代理：git config --global https.proxy http://127.0.0.1:7897

::	取消代理：git config --global --unset http.proxy
::	取消代理：git config --global --unset https.proxy
::	在新目录或新电脑先：git clone https://github.com/350030173/ALDB.git
::	
::	如果是国内的 gitee 不能用代理

git add -A							
set /p "UpdateNotes=输入更新说明: "
git commit -m '%UpdateNotes%'		
git push origin main				

pause
