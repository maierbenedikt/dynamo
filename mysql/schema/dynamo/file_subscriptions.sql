CREATE TABLE `file_subscriptions` (
  `id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `file_id` bigint(20) unsigned NOT NULL,
  `site_id` int(11) unsigned NOT NULL,
  `status` enum('new','inbatch','done','retry','held') CHARACTER SET latin1 COLLATE latin1_general_ci NOT NULL DEFAULT 'new',
  `created` datetime NOT NULL,
  `last_update` datetime DEFAULT NULL,
  `delete` tinyint(1) unsigned NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `subscription` (`file_id`,`site_id`,`delete`),
  KEY `delete` (`delete`)
) ENGINE=MyISAM DEFAULT CHARSET=latin1 CHECKSUM=1;
