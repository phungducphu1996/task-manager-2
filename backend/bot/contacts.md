# Hazel Contact Registry

File này được sync từ database user và env group của Task Manager.
Custom prompt riêng nằm ở từng file được link ở từng contact; bot sẽ đọc các file đó khi chat hoặc gửi notification.

## Personal Contacts

- Sẽ được sync từ `social.users` khi bot chạy.

## Group Contacts

- Sẽ được sync từ `ZALO_GROUP_ID` và `ZALO_ALLOWED_GROUP_IDS` khi bot chạy.
