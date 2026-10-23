class BlogMedia {
  factory BlogMedia.fromJson(Map data) {
    final detail = data['media'];
    return BlogMedia._(detail is Map ? {...data, ...detail} : data);
  }
  BlogMedia._(Map data)
    : id = data['id'] as String,
      status = data['status'] as String? ?? 'ready',
      url = data['url'] as String?,
      thumbnailUrl = data['thumbnail_url'] as String?,
      durationSeconds = (data['duration_seconds'] as num?)?.round(),
      width = (data['width'] as num?)?.toInt(),
      height = (data['height'] as num?)?.toInt(),
      sourceBytes = (data['source_bytes'] as num?)?.toInt(),
      errorCode = data['error_code'] as String?;
  final String id, status;
  final String? url, thumbnailUrl, errorCode;
  final int? durationSeconds, width, height, sourceBytes;
  bool get ready => status == 'ready' && url != null;
  bool get processing =>
      const ['uploading', 'queued', 'processing'].contains(status);
  String get statusLabel => switch (status) {
    'uploading' => 'Загрузка видео',
    'queued' => 'Видео в очереди на обработку',
    'processing' => 'Подготавливаем видео для просмотра',
    'ready' => 'Видео готово',
    'failed' => blogMediaErrorMessage(errorCode) ?? 'Не удалось обработать видео. Выбери другой файл или загрузи его снова.',
    _ => 'Видео недоступно',
  };
}

String? blogMediaErrorMessage(String? code) => switch (code) {
  'video_size_limit' || 'processed_size_limit' || 'processing_output_limit' =>
    'Видео слишком большое. Выбери более короткий файл до 100 МБ.',
  'video_too_long' =>
    'Видео длиннее 3 минут. Сократи его и выбери файл заново.',
  'media_quota_exceeded' => 'Достигнут лимит хранения видео. Сейчас новое видео загрузить нельзя; попробуй позже.',
  'media_queue_full' =>
    'Очередь обработки заполнена. Подожди немного и повтори загрузку.',
  'media_storage_full' ||
  'media_storage_unavailable' ||
  'media_tools_unavailable' =>
    'Сервис видео временно недоступен. Повтори попытку позже.',
  'mp4_or_mov_required' ||
  'unsupported_video' ||
  'invalid_video' ||
  'incomplete_video' =>
    'Не удалось прочитать это видео. Выбери исправный файл MP4 или MOV.',
  'processing_timeout' || 'processing_failed' || 'thumbnail_failed' =>
    'Обработка видео не завершилась. Повтори загрузку или выбери другой файл.',
  'source_size_mismatch' ||
  'source_hash_mismatch' ||
  'upload_length_mismatch' ||
  'upload_id_conflict' =>
    'Файл изменился или загрузился не целиком. Выбери его заново.',
  'upload_cancelled' || 'upload_timeout' || 'upload_interrupted' =>
    'Загрузка прервалась. Выбери видео заново и повтори попытку.',
  'media_in_use' =>
    'Это видео используется в публикации. Сначала измени или сними публикацию.',
  'media_not_found' => 'Видео больше недоступно. Выбери файл заново.',
  _ => null,
};
