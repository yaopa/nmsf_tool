# nmsf_tool
A Python program used to handle the cache files in the "Music3" folder of NetEase Cloud Music

一个用于处理网易云音乐"Music3"文件夹下缓存的python程序
## 使用方式
1、克隆该库到本地，部署好环境，如果遇到软件包缺失，使用pip下载该缺失包。

2、使用USB线连接手机，打开APP的数据文件夹。

3、进入file\Cache,将Music3文件夹完整的复制出来，放在克隆好的文件夹里（就是nmsf_tool文件夹）

4、使用编辑器，编辑好main.py文件开头的配置信息

5、运行该配置文件

## 程序特点
不同于ncm文件，nmsf文件只使用了异或加密，和uc相似,但是uc不进行切片和MD5验证。

nmsfi文件包含了切片数据，切片大小，config文件中包含的音频文件正确的后缀，文件名本身包含MD5。

本程序通过切片大小验证、MD5验证，实现了对缓存文件的准确解密。并写入元数据，方便播放。

还包含日志、进度条、API调用缓存等功能，实时了解进度、减少API调用次数。

## 其他
实际上，该程序使用了AI编写，编写后进行了核查，但然而可能存在问题。

如果遇到问题，请先查看日志信息。确定是本程序问题，请在Issues反馈。
