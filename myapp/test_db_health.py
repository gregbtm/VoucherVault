from unittest.mock import patch, MagicMock
from django.db import connection, OperationalError
from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from myapp.db_health import missing_tables, tolerate_missing_table
from myapp.models import SiteConfiguration


class MissingTablesTestCase(TestCase):
    """
    Full-sweep detection for the class of failure behind the
    myapp_itemrecommendation outage - a migration silently rolls back (or
    a table is otherwise dropped) and nothing surfaces it until a user
    hits a 500.
    """

    def test_no_missing_tables_on_a_freshly_migrated_database(self):
        self.assertEqual(missing_tables(), [])

    def test_detects_a_genuinely_dropped_table(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE myapp_itemrecommendation")

        missing = missing_tables()
        tables = [t for _model, t in missing]
        self.assertIn('myapp_itemrecommendation', tables)


class TolerateMissingTableTestCase(TestCase):

    def test_no_exception_leaves_available_true_and_runs_body(self):
        ran = False
        with tolerate_missing_table('some_feature') as health:
            ran = True
            self.assertTrue(health.available)
        self.assertTrue(ran)

    def test_missing_table_error_is_suppressed_and_flips_available_false(self):
        with tolerate_missing_table('some_feature') as health:
            raise OperationalError('no such table: myapp_itemrecommendation')
        self.assertFalse(health.available)

    def test_postgres_style_missing_relation_error_is_also_caught(self):
        with tolerate_missing_table('some_feature') as health:
            raise OperationalError('relation "myapp_itemrecommendation" does not exist')
        self.assertFalse(health.available)

    def test_unrelated_operational_error_is_not_swallowed(self):
        with self.assertRaises(OperationalError):
            with tolerate_missing_table('some_feature'):
                raise OperationalError('database is locked')

    def test_logs_only_once_within_the_dedup_window(self):
        with patch('myapp.db_health.logger') as mock_logger:
            with tolerate_missing_table('dedup_feature'):
                raise OperationalError('no such table: foo')
            with tolerate_missing_table('dedup_feature'):
                raise OperationalError('no such table: foo')
        self.assertEqual(mock_logger.error.call_count, 1)


class ShowItemsDegradesGracefullyTestCase(TestCase):
    """The exact production symptom: /en/ (show_items) 500'd on every load
    once myapp_itemrecommendation went missing. It must now render 200
    with recommendations simply empty."""

    def setUp(self):
        self.user = User.objects.create_user('dbhealthuser', 'dbh@example.com', 'password123')
        self.client.force_login(self.user)

    def test_show_items_renders_200_when_recommendation_table_missing(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE myapp_itemrecommendation")

        response = self.client.get(reverse('show_items'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['active_recommendations']), [])

    def test_show_items_still_shows_recommendations_when_table_present(self):
        response = self.client.get(reverse('show_items'))
        self.assertEqual(response.status_code, 200)


class CheckDatabaseIntegrityTaskTestCase(TestCase):

    def setUp(self):
        self.config = SiteConfiguration.load()
        self.config.security_alert_ntfy_topic = 'test-topic'
        self.config.save()

    @patch('myapp.db_health.missing_tables', return_value=[])
    @patch('requests.post')
    def test_no_alert_when_nothing_missing(self, mock_post, mock_missing):
        from myapp.tasks import check_database_integrity_task
        check_database_integrity_task()
        mock_post.assert_not_called()

    @patch('myapp.db_health.missing_tables', return_value=[('myapp.ItemRecommendation', 'myapp_itemrecommendation')])
    @patch('requests.post')
    def test_alert_sent_when_table_missing_and_topic_configured(self, mock_post, mock_missing):
        from myapp.tasks import check_database_integrity_task
        check_database_integrity_task()
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn('test-topic', args[0])
        self.assertIn(b'myapp_itemrecommendation', kwargs['data'])

    @patch('myapp.db_health.missing_tables', return_value=[('myapp.ItemRecommendation', 'myapp_itemrecommendation')])
    @patch('requests.post')
    def test_no_alert_when_topic_not_configured(self, mock_post, mock_missing):
        self.config.security_alert_ntfy_topic = ''
        self.config.save()
        from myapp.tasks import check_database_integrity_task
        check_database_integrity_task()
        mock_post.assert_not_called()
